#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Euro-Office contributors
# SPDX-License-Identifier: Apache-2.0
"""Entrypoint contract check: does the real DocumentServer entrypoint accept the
environment this chart gives each component?

For each case the chart is rendered and, for the docservice, converter and
adminpanel containers, the effective environment is built from the ConfigMaps in
`envFrom` plus the literal `env` entries (secret references get a dummy value, LOG_*
keys are dropped). The orchestrated `docker-entrypoint.sh` of DocumentServer then
runs with ENTRYPOINT_CONFIG_ONLY=true and must exit 0. The entrypoint is a
configuration generator that validates the Redis settings of every service mode,
so this catches an environment that a component would crash on at start.
The Converter must not receive any Redis secret reference.

    git clone --depth 1 --filter=blob:none --sparse https://github.com/Euro-Office/DocumentServer ds
    git -C ds sparse-checkout set build/scripts
    python3 -I tests/entrypoint_contract.py --ds-scripts ds/build/scripts

Needs Python 3, PyYAML, helm, bash and jq. No network. Secret values and the
generated NODE_CONFIG are never printed.
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

COMPONENTS = ("docservice", "converter", "adminpanel")
SECRET_KEY = re.compile(r"(PWD|PASS|SECRET|TOKEN|KEY)", re.IGNORECASE)

CASES = {
    "standalone, no user": {
        "docservice": {"editorDataStorage": "standalone"},
        "connections": {"redisUser": "", "redisPassword": "pw"},
    },
    "standalone, appuser": {
        "docservice": {"editorDataStorage": "standalone"},
        "connections": {"redisUser": "appuser", "redisPassword": "pw"},
    },
    "sentinel, no auth": {
        "docservice": {"editorDataStorage": "sentinel"},
        "connections": {"redisNoPass": True, "redisSentinelNodes": ["s1:26379", "s2:26379"]},
    },
    "sentinel, password only": {
        "docservice": {"editorDataStorage": "sentinel"},
        "connections": {
            "redisPassword": "pw",
            "redisSentinelNodes": ["s1:26379"],
            "redisSentinelNoPass": False,
            "redisSentinelPassword": "x",
            "redisSentinelUser": "",
        },
    },
    "sentinel, sentuser + password": {
        "docservice": {"editorDataStorage": "sentinel"},
        "connections": {
            "redisPassword": "pw",
            "redisSentinelNodes": ["s1:26379"],
            "redisSentinelNoPass": False,
            "redisSentinelPassword": "x",
            "redisSentinelUser": "sentuser",
        },
    },
    "cluster, appuser": {
        "docservice": {"editorDataStorage": "cluster"},
        "connections": {"redisPassword": "pw", "redisUser": "appuser", "redisClusterNodes": ["c1:6379", "c2:6379"]},
    },
    "cluster + TLS": {
        "docservice": {"editorDataStorage": "cluster"},
        "connections": {"redisPassword": "pw", "redisClusterNodes": ["c1:6379"], "redisTLS": True},
    },
    "memory (default)": {},
    "data memory, stat standalone": {
        "docservice": {"editorDataStorage": "memory", "editorStatStorage": "standalone"},
        "connections": {"redisPassword": "pw"},
    },
}


def merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = value
    return base


def render(chart, values):
    """Render the chart with the case values (adminpanel enabled); returns the documents."""
    values = merge({"adminpanel": {"enabled": True}}, values)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "values.yaml"
        path.write_text(yaml.safe_dump(values))
        result = subprocess.run(
            ["helm", "template", "contract", str(chart), "-f", str(path)],
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        raise RuntimeError("helm template failed: " + first_line(result.stderr))
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def first_line(text, secrets=()):
    for line in text.splitlines():
        line = line.strip()
        if line:
            for secret in secrets:
                line = line.replace(secret, "***")
            return line[:140]
    return ""


def workload(docs, name):
    for doc in docs:
        if doc["kind"] in ("Deployment", "StatefulSet") and doc["metadata"]["name"] == name:
            return doc
    return None


def effective_env(docs, container):
    """Non-secret environment of a container: envFrom ConfigMaps, then literal env.

    Returns (env, redis_secret_refs). Secret references get a dummy value; the keys of
    an envFrom Secret are not known, so it adds nothing to the environment."""
    configmaps = {d["metadata"]["name"]: d.get("data", {}) for d in docs if d["kind"] == "ConfigMap"}
    env = {}
    refs = []
    for source in container.get("envFrom") or []:
        ref = source.get("configMapRef")
        if ref:
            env.update({k: str(v) for k, v in configmaps.get(ref["name"], {}).items()})
        elif source.get("secretRef") and "redis" in source["secretRef"]["name"]:
            # The keys of a Secret are not in the render: only a Redis Secret on a component is reported.
            refs.append("envFrom:" + source["secretRef"]["name"])
    for entry in container.get("env") or []:
        if "value" in entry:
            env[entry["name"]] = str(entry["value"])
        else:
            env[entry["name"]] = "dummy"
            if entry["name"].startswith("REDIS_"):
                refs.append(entry["name"])
    # LOG_* make the entrypoint edit log4js files that exist only in the image.
    return {k: v for k, v in env.items() if not k.startswith("LOG_")}, refs


def run_entrypoint(entrypoint, mode, env, tmp):
    out = Path(tmp) / ("node-config-%s.json" % mode)  # must not exist: the entrypoint creates it
    process_env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin",
        "COMPANY_NAME": "eurooffice",
        "JWT_ENABLED": "false",
        "NODE_CONFIG_DIR": str(Path(tmp) / "no-config"),
        **env,
        "ENTRYPOINT_CONFIG_ONLY": "true",
        "ENTRYPOINT_CONFIG_ONLY_OUTPUT": str(out),
    }
    result = subprocess.run(
        ["bash", str(entrypoint), mode],
        env=process_env,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp,
    )
    secrets = [v for k, v in env.items() if SECRET_KEY.search(k) and len(v) > 3]
    return result.returncode, first_line(result.stderr or result.stdout, secrets)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--chart", default=str(Path(__file__).resolve().parents[1]), help="chart path (default: repository root)")
    parser.add_argument("--ds-scripts", required=True, help="path to DocumentServer build/scripts")
    parser.add_argument("--case", help="run one case by name")
    args = parser.parse_args()

    entrypoint = Path(args.ds_scripts) / "orchestrated" / "docker-entrypoint.sh"
    if not entrypoint.is_file():
        sys.exit("entrypoint not found: %s" % entrypoint)
    cases = CASES
    if args.case:
        if args.case not in CASES:
            sys.exit("unknown case %r; cases: %s" % (args.case, ", ".join(CASES)))
        cases = {args.case: CASES[args.case]}

    rows, failed = [], False
    for name, values in cases.items():
        try:
            docs = render(args.chart, values)
        except RuntimeError as error:
            rows.append((name, "(render)", "FAIL", str(error)))
            failed = True
            continue
        for component in COMPONENTS:
            doc = workload(docs, component)
            if doc is None:
                rows.append((name, component, "FAIL", "workload not rendered"))
                failed = True
                continue
            containers = doc["spec"]["template"]["spec"]["containers"]
            container = next((c for c in containers if c["name"] == component), None)
            if container is None:
                rows.append((name, component, "FAIL", "container not found"))
                failed = True
                continue
            mode = (container.get("args") or [component])[0]
            env, refs = effective_env(docs, container)
            if component == "converter" and refs:
                rows.append((name, component, "FAIL", "Redis secret refs on the Converter: " + ",".join(refs)))
                failed = True
                continue
            with tempfile.TemporaryDirectory() as tmp:
                rc, error = run_entrypoint(entrypoint, mode, env, tmp)
            rows.append((name, component, str(rc), error))
            failed = failed or rc != 0

    print("%-32s %-12s %-5s %s" % ("case", "component", "rc", "first error line"))
    for name, component, rc, error in rows:
        print("%-32s %-12s %-5s %s" % (name, component, rc, error))
    if failed:
        print("\nFAILED: the entrypoint rejects the environment of at least one component")
        return 1
    print("\nOK: %d cases x %d components" % (len(cases), len(COMPONENTS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
