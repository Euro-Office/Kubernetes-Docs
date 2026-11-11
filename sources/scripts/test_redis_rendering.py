#!/usr/bin/env python3
"""Redis authentication and TLS chart regression tests. Run with Python 3, PyYAML and Helm:

    python3 sources/scripts/test_redis_rendering.py
"""

import itertools
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


CHART = Path(__file__).resolve().parents[2]
AUTH_MODES = {"none": "ignored-user", "password": "", "acl": "app-user"}


def render(values):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as overrides:
        json.dump(values, overrides)
        overrides.flush()
        return subprocess.run(
            ["helm", "template", "redis-auth-test", str(CHART), "-f", overrides.name],
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
                result = render({"connections": {
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
            "docservice": {"extraEnvVars": [override]},
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


if __name__ == "__main__":
    unittest.main()
