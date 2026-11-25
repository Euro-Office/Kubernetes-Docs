#!/usr/bin/env python3
"""Redis authentication and TLS chart regression tests. Run with Python 3, PyYAML and Helm:

    python3 sources/scripts/test_redis_rendering.py
"""

import itertools
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml


CHART = Path(__file__).resolve().parents[2]
AUTH_MODES = {"none": "ignored-user", "password": "", "acl": "app-user"}


def render(values, chart=CHART):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as overrides:
        json.dump(values, overrides)
        overrides.flush()
        return subprocess.run(
            ["helm", "template", "redis-auth-test", str(chart), "-f", overrides.name],
            text=True, capture_output=True, check=False,
        )


def environment_lists(value):
    if isinstance(value, dict):
        if "env" in value:
            yield value["env"]
        for child in value.values():
            yield from environment_lists(child)
    elif isinstance(value, list):
        for child in value:
            yield from environment_lists(child)


class RedisAuthenticationRendering(unittest.TestCase):
    def documents(self, values):
        result = render(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [doc for doc in yaml.safe_load_all(result.stdout) if doc]

    def test_username_defaults(self):
        defaults = yaml.safe_load((CHART / "values.yaml").read_text())["connections"]
        self.assertEqual(defaults["redisUser"], "default")
        self.assertEqual(defaults["redisSentinelUser"], "")
        documents = self.documents({})
        config = next(doc["data"] for doc in documents if doc["metadata"]["name"] == "documentserver")
        self.assertEqual(config["REDIS_SERVER_USER"], "default")
        self.assertNotIn("REDIS_SENTINEL_USER", config)

    def test_authentication_matrix(self):
        for topology, node_mode, source in itertools.product(
            ("standalone", "sentinel", "cluster"), AUTH_MODES, ("inline", "existing")
        ):
            sentinel_modes = AUTH_MODES if topology == "sentinel" else ("none",)
            for sentinel_mode in sentinel_modes:
                with self.subTest(topology=topology, node=node_mode, sentinel=sentinel_mode, source=source):
                    connections = {}
                    for prefix, mode, password in (
                        ("redis", node_mode, "node-password"),
                        ("redisSentinel", sentinel_mode, "sentinel-password"),
                    ):
                        connections.update({
                            prefix + "User": AUTH_MODES[mode],
                            prefix + "NoPass": mode == "none",
                            prefix + "Password": password if source == "inline" else "",
                            prefix + "ExistingSecret": "external-" + prefix,
                        })
                    if topology == "sentinel":
                        connections["redisSentinelNodes"] = ["sentinel-a:26379", "sentinel-b:26379"]
                    elif topology == "cluster":
                        connections["redisClusterNodes"] = ["redis-a:6379", "redis-b:6379"]
                    documents = self.documents({
                        "connections": connections,
                        "docservice": {"editorDataStorage": topology},
                    })
                    config = next(doc["data"] for doc in documents if doc["metadata"]["name"] == "documentserver")
                    secrets = {doc["metadata"]["name"]: doc for doc in documents if doc["kind"] == "Secret"}
                    for env_prefix, prefix, mode, password in (
                        ("REDIS_SERVER", "redis", node_mode, "node-password"),
                        ("REDIS_SENTINEL", "redisSentinel", sentinel_mode, "sentinel-password"),
                    ):
                        enabled = mode != "none" and (prefix == "redis" or topology == "sentinel")
                        if enabled and AUTH_MODES[mode]:
                            self.assertEqual(config[env_prefix + "_USER"], AUTH_MODES[mode])
                        else:
                            self.assertNotIn(env_prefix + "_USER", config)
                        refs = [entry["valueFrom"]["secretKeyRef"]
                                for env in environment_lists(documents)
                                for entry in env if entry["name"] == env_prefix + "_PWD"]
                        self.assertEqual(bool(refs), enabled)
                        created_name = "redis-auth-test-" + ("redis" if prefix == "redis" else "redis-sentinel")
                        self.assertEqual(created_name in secrets, enabled and source == "inline")
                        for ref in refs:
                            if source == "existing":
                                self.assertEqual(ref["name"], "external-" + prefix)
                            else:
                                self.assertEqual(ref["name"], created_name)
                                self.assertEqual(secrets[ref["name"]]["stringData"][ref["key"]], password)
                    for kind in ("Pod", "ConfigMap"):
                        self.assertTrue(any(doc["kind"] == kind and doc["metadata"]["name"] == "test-redis-config"
                                            for doc in documents))

    def test_missing_required_passwords(self):
        for topology, username in itertools.product(("standalone", "sentinel", "cluster"), ("", "app-user")):
            connections = {"redisUser": username, "redisExistingSecret": "", "redisPassword": ""}
            if topology == "sentinel":
                connections["redisSentinelNodes"] = ["sentinel:26379"]
            elif topology == "cluster":
                connections["redisClusterNodes"] = ["redis:6379"]
            with self.subTest(topology=topology, username=username):
                result = render({"connections": connections, "docservice": {"editorDataStorage": topology}})
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr, "Authenticated Redis requires connections.redisPassword or connections.redisExistingSecret|A Redis Password is required!")
        for node_no_pass, username in itertools.product((True, False), ("", "sentinel-user")):
            with self.subTest(node_no_pass=node_no_pass, sentinel_username=username):
                result = render({"docservice": {"editorDataStorage": "sentinel"}, "connections": {
                    "redisNoPass": node_no_pass,
                    "redisSentinelNodes": ["sentinel:26379"],
                    "redisSentinelUser": username,
                    "redisSentinelNoPass": False,
                    "redisSentinelPassword": "",
                    "redisSentinelExistingSecret": "",
                }})
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr, "Authenticated Redis Sentinel requires connections.redisSentinelPassword or connections.redisSentinelExistingSecret|A Redis Sentinel Password is required!")

    def test_no_pass_without_passwords(self):
        self.documents({"connections": {
            "redisNoPass": True, "redisPassword": "", "redisExistingSecret": "",
            "redisSentinelNodes": ["sentinel:26379"],
            "redisSentinelNoPass": True, "redisSentinelPassword": "", "redisSentinelExistingSecret": "",
        }})

    def test_configuration_hook_selection(self):
        for storage, enabled in (("memory", True), ("standalone", False)):
            documents = self.documents({"docservice": {"editorDataStorage": storage}, "tests": {"enabled": enabled}})
            self.assertFalse(any(doc["metadata"]["name"] == "test-redis-config" for doc in documents))
        documents = self.documents({"docservice": {"editorStatStorage": "standalone"}})
        self.assertTrue(any(doc["kind"] == "Pod" and doc["metadata"]["name"] == "test-redis-config" for doc in documents))

    def test_configuration_hook_pod(self):
        def pod(values):
            documents = self.documents({"docservice": {"editorDataStorage": "standalone", **values.pop("docservice", {})}, **values})
            return next(doc for doc in documents if doc["kind"] == "Pod" and doc["metadata"]["name"] == "test-redis-config")

        default = pod({})
        self.assertNotIn("activeDeadlineSeconds", default["spec"])
        self.assertEqual(default["metadata"]["annotations"]["helm.sh/hook-delete-policy"], "before-hook-creation")
        container = default["spec"]["containers"][0]
        names = [entry["name"] for entry in container["env"]]
        self.assertNotIn("ENTRYPOINT_CONFIG_ONLY", names)
        self.assertNotIn("FOO", names)
        self.assertNotIn("custom-file", [mount["name"] for mount in container["volumeMounts"]])
        self.assertNotIn("custom-file", [volume["name"] for volume in default["spec"]["volumes"]])

        custom = pod({"extraConf": {"configMap": "my-conf"},
                      "docservice": {"extraEnvVars": [{"name": "FOO", "value": "bar"}]}})
        container = custom["spec"]["containers"][0]
        self.assertIn({"name": "FOO", "value": "bar"}, container["env"])
        self.assertIn({"name": "custom-file", "mountPath": "/etc/euro-office/documentserver/local.json",
                       "subPath": "local.json"}, container["volumeMounts"])
        self.assertIn({"name": "custom-file", "configMap": {"name": "my-conf"}}, custom["spec"]["volumes"])

    def test_connectivity_hook_ca_mounts(self):
        extras = {
            "extraEnvVars": [
                {"name": "REDIS_SERVER_CA_CERTS", "value": "/etc/redis-ca/node.crt"},
                {"name": "REDIS_SENTINEL_CA_CERTS", "value": "/etc/redis-ca/sentinel.crt"},
            ],
            "extraVolumes": [{"name": "redis-ca", "secret": {"secretName": "private-redis-ca"}}],
            "extraVolumeMounts": [{"name": "redis-ca", "mountPath": "/etc/redis-ca", "readOnly": True}],
        }
        for storage_s3 in (False, True):
            with self.subTest(storage_s3=storage_s3):
                documents = self.documents({
                    "tests": extras, "persistence": {"storageS3": storage_s3},
                    "docservice": {"editorDataStorage": "sentinel"},
                    "connections": {"redisSentinelNodes": ["sentinel:26379"],
                                    "redisTLS": True, "redisSentinelTLS": True,
                                    "redisCAExistingSecret": "shared-ca"},
                })
                pod = next(doc for doc in documents if doc["kind"] == "Pod"
                           and doc["metadata"]["name"] == "test-ds")
                container = pod["spec"]["containers"][0]
                for entry in extras["extraEnvVars"]:
                    self.assertIn(entry, container["env"])
                    self.assertEqual(sum(e["name"] == entry["name"] for e in container["env"]), 1)
                self.assertIn(extras["extraVolumes"][0], pod["spec"]["volumes"])
                self.assertIn(extras["extraVolumeMounts"][0], container["volumeMounts"])
                self.assertIn("test-ds", [v["name"] for v in pod["spec"]["volumes"]])
                self.assertEqual("ds-files" in [v["name"] for v in pod["spec"]["volumes"]], not storage_s3)

    def test_shared_ca_for_applications_and_connectivity_hook(self):
        ca_path = "/etc/redis-ca-certificates/ca.crt"
        for topology, node_tls, sentinel_tls, secret in itertools.product(
            ("standalone", "sentinel", "cluster"), (False, True), (False, True),
            ("", "{{ .Release.Name }}-ca"),
        ):
            with self.subTest(topology=topology, node_tls=node_tls, sentinel_tls=sentinel_tls, secret=secret):
                connections = {"redisTLS": node_tls, "redisSentinelTLS": sentinel_tls,
                               "redisCAExistingSecret": secret}
                if topology == "sentinel":
                    connections["redisSentinelNodes"] = ["sentinel:26379"]
                elif topology == "cluster":
                    connections["redisClusterNodes"] = ["redis:6379"]
                documents = self.documents({
                    "connections": connections,
                    "docservice": {"editorDataStorage": topology},
                    "adminpanel": {"enabled": True},
                })
                enabled = bool(secret and (node_tls or sentinel_tls))
                found = set()
                for doc in documents:
                    spec = doc.get("spec", {})
                    spec = spec.get("template", {}).get("spec", spec)
                    for container in spec.get("containers", []):
                        component = container["name"]
                        if component not in ("docservice", "converter", "adminpanel", "test-ds"):
                            continue
                        found.add(component)
                        env = {e["name"]: e for e in container.get("env", [])}
                        volumes = [v for v in spec["volumes"] if v["name"] == "redis-ca-certificates"]
                        mounts = [v for v in container["volumeMounts"] if v["name"] == "redis-ca-certificates"]
                        self.assertEqual(bool(volumes), enabled)
                        self.assertEqual(bool(mounts), enabled)
                        if enabled:
                            self.assertEqual(volumes[0]["secret"], {
                                "secretName": "redis-auth-test-ca", "items": [{"key": "ca.crt", "path": "ca.crt"}],
                            })
                            self.assertEqual(mounts[0]["mountPath"], str(Path(ca_path).parent))
                            self.assertIs(mounts[0]["readOnly"], True)
                        expected_env = ({"REDIS_SERVER_CA_CERTS": enabled and node_tls,
                                         "REDIS_SENTINEL_CA_CERTS": enabled and sentinel_tls}
                                        if component == "test-ds" else {"NODE_EXTRA_CA_CERTS": enabled})
                        for key, present in expected_env.items():
                            self.assertEqual(key in env, present)
                            if present:
                                self.assertEqual(env[key]["value"], ca_path)
                self.assertEqual(found, {"docservice", "converter", "adminpanel", "test-ds"})

    def test_shared_ca_respects_application_overrides(self):
        override = {"name": "NODE_EXTRA_CA_CERTS", "value": "/existing/ca.pem"}
        documents = self.documents({
            "connections": {"redisTLS": True, "redisCAExistingSecret": "redis-ca",
                            "redisCASecretKeyName": "trust.pem"},
            "adminpanel": {"enabled": True, "extraEnvVars": [override]},
            "docservice": {"editorDataStorage": "standalone", "extraEnvVars": [override]},
            "converter": {"extraEnvVars": [override]},
        })
        for doc in documents:
            if doc["kind"] not in ("Deployment", "StatefulSet"):
                continue
            spec = doc["spec"]["template"]["spec"]
            for container in spec["containers"]:
                if container["name"] in ("docservice", "converter", "adminpanel"):
                    self.assertEqual([e for e in container["env"] if e["name"] == override["name"]], [override])
                    volume = next(v for v in spec["volumes"] if v["name"] == "redis-ca-certificates")
                    self.assertEqual(volume["secret"]["items"], [{"key": "trust.pem", "path": "ca.crt"}])


class RedisNotUsedInMemoryMode(unittest.TestCase):
    """Memory editor storage needs no Redis Secret, credentials or CA wiring."""

    WORKLOADS = ("docservice", "converter", "adminpanel", "test-ds")
    FORBIDDEN_ENV = ("REDIS_SERVER_PWD", "REDIS_SENTINEL_PWD", "NODE_EXTRA_CA_CERTS",
                     "REDIS_SERVER_CA_CERTS", "REDIS_SENTINEL_CA_CERTS")

    def documents(self, values):
        values = {"adminpanel": {"enabled": True}, **values}
        result = render(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout, [doc for doc in yaml.safe_load_all(result.stdout) if doc]

    def workloads(self, documents):
        found = {}
        for doc in documents:
            name = doc["metadata"]["name"]
            if doc["kind"] in ("Deployment", "StatefulSet", "Pod") and name in self.WORKLOADS:
                found[name] = doc
        self.assertEqual(set(found), set(self.WORKLOADS))
        return found

    def env_names(self, doc):
        return {entry["name"] for env in environment_lists(doc) for entry in env or []}

    def assert_no_redis(self, values):
        text, documents = self.documents(values)
        for name, doc in self.workloads(documents).items():
            self.assertFalse(self.env_names(doc) & set(self.FORBIDDEN_ENV), name)
        self.assertNotIn("redis-ca-certificates", text)
        self.assertNotIn("kind: Secret\nmetadata:\n  name: t-redis", text)
        secrets = [doc["metadata"]["name"] for doc in documents if doc["kind"] == "Secret"]
        self.assertFalse([name for name in secrets if "redis" in name], secrets)

    def test_memory_default(self):
        self.assert_no_redis({})

    def test_memory_ignores_secrets_and_tls(self):
        self.assert_no_redis({"connections": {"redisPassword": "pw", "redisSentinelPassword": "spw",
                                              "redisSentinelNodes": ["sentinel:26379"],
                                              "redisSentinelNoPass": False,
                                              "redisTLS": True, "redisSentinelTLS": True,
                                              "redisCAExistingSecret": "my-ca"}})

    def test_memory_without_any_password(self):
        result = render({"connections": {"redisNoPass": False, "redisPassword": "", "redisExistingSecret": "",
                                         "redisSentinelNodes": ["sentinel:26379"], "redisSentinelNoPass": False,
                                         "redisSentinelPassword": "", "redisSentinelExistingSecret": ""}})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_redis_modes_keep_env(self):
        for values in ({"docservice": {"editorDataStorage": "standalone"}},
                       {"docservice": {"editorDataStorage": "memory", "editorStatStorage": "standalone"}}):
            with self.subTest(values=values):
                _, documents = self.documents(values)
                for name, doc in self.workloads(documents).items():
                    self.assertIn("REDIS_SERVER_PWD", self.env_names(doc), name)

    def test_redis_mode_still_requires_password(self):
        result = render({"docservice": {"editorDataStorage": "standalone"},
                         "connections": {"redisNoPass": False, "redisPassword": "", "redisExistingSecret": ""}})
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stderr, "Authenticated Redis requires connections.redisPassword or connections.redisExistingSecret|A Redis Password is required!")

    def test_no_whitespace_only_lines_in_memory_mode(self):
        text, _ = self.documents({})
        for source in ("deployments/docservice.yaml", "deployments/converter.yaml", "statefulset/adminpanel.yaml",
                       "tests/test-ds-pod.yaml"):
            part = text.split("# Source: docs/templates/" + source)[1].split("\n# Source:")[0]
            blank = [n for n, line in enumerate(part.splitlines()) if line and not line.strip()]
            self.assertFalse(blank, "%s has whitespace-only lines %s" % (source, blank))


class RedisImageVersionGate(unittest.TestCase):
    """The gate is inactive in the repository; a copy with a fake minimum version exercises it."""

    MINIMUM = "99.0.0"
    STORAGE = {
        "standalone": {"docservice": {"editorDataStorage": "standalone"}},
        "sentinel": {"docservice": {"editorDataStorage": "sentinel"},
                     "connections": {"redisSentinelNodes": ["sentinel:26379"]}},
        "cluster": {"docservice": {"editorDataStorage": "cluster"},
                    "connections": {"redisClusterNodes": ["redis:6379"]}},
    }

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.chart = Path(cls.temp.name) / "chart"
        shutil.copytree(CHART, cls.chart, ignore=shutil.ignore_patterns(".git"))
        helpers = cls.chart / "templates" / "_helpers.tpl"
        text, count = re.subn(
            r'\{\{-\s*define "ds\.redis\.minImageVersion"\s*-\}\}\s*\{\{-\s*end\s*-\}\}',
            '{{- define "ds.redis.minImageVersion" -}}' + cls.MINIMUM + '{{- end -}}',
            helpers.read_text(),
        )
        assert count == 1, "the minimum version helper was not found"
        helpers.write_text(text)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def result(self, values, tag, chart=None, docservice_tag=None):
        values = json.loads(json.dumps(values))
        values.setdefault("images", {})["tag"] = tag
        if docservice_tag:
            values.setdefault("docservice", {}).setdefault("image", {})["tag"] = docservice_tag
        return render(values, chart or self.chart)

    def assertRejected(self, values, tag, **kwargs):
        result = self.result(values, tag, **kwargs)
        self.assertNotEqual(result.returncode, 0, result.stdout[:200])
        self.assertIn("requires DocumentServer %s or later" % self.MINIMUM, result.stderr)

    def assertRenders(self, values, tag, **kwargs):
        result = self.result(values, tag, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_inactive_without_minimum_version(self):
        self.assertRenders(self.STORAGE["standalone"], "v9.3.8", chart=CHART)
        self.assertRenders({"connections": {"redisTLS": True}}, "v9.3.8", chart=CHART)
        self.assertRenders({"connections": {"redisSentinelTLS": True}}, "v9.3.8", chart=CHART)

    def test_old_tag_rejected_when_redis_wanted(self):
        for name, values in self.STORAGE.items():
            with self.subTest(storage=name):
                self.assertRejected(values, "v9.3.8")

    def test_old_tag_allowed_with_memory(self):
        self.assertRenders({}, "v9.3.8")
        # TLS settings have no effect without a Redis storage mode.
        self.assertRenders({"connections": {"redisTLS": True}}, "v9.3.8")
        self.assertRenders({"connections": {"redisSentinelTLS": True}}, "v9.3.8")

    def test_new_and_prerelease_tags(self):
        for tag in ("v99.0.0", "99.0.0-rc.1", "v99.1.0", "100.0.0"):
            with self.subTest(tag=tag):
                self.assertRenders(self.STORAGE["standalone"], tag)

    def test_non_numeric_tags_not_checked(self):
        for tag in ("latest", "nightly", "dev-redis-abc123"):
            with self.subTest(tag=tag):
                self.assertRenders(self.STORAGE["standalone"], tag)

    def test_docservice_tag_wins(self):
        self.assertRejected(self.STORAGE["standalone"], "v99.0.0", docservice_tag="v9.3.8")
        self.assertRenders(self.STORAGE["standalone"], "v9.3.8", docservice_tag="v99.0.0")


if __name__ == "__main__":
    unittest.main()
