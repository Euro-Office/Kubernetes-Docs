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
        # The usernames are env entries of the Redis-using Pods, not ConfigMap data (see RedisUsersInEnv).
        documents = self.documents({"docservice": {"editorDataStorage": "standalone"}})
        config = next(doc["data"] for doc in documents if doc["metadata"]["name"] == "documentserver")
        self.assertNotIn("REDIS_SERVER_USER", config)
        docservice = next(doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "docservice")
        env = {e["name"]: e.get("value") for env in environment_lists(docservice) for e in env}
        self.assertEqual(env["REDIS_SERVER_USER"], "default")
        self.assertNotIn("REDIS_SENTINEL_USER", env)

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
                    self.assertFalse({"REDIS_SERVER_USER", "REDIS_SENTINEL_USER"} & set(config))
                    # The usernames are env entries of the Pods that use Redis (see RedisUsersInEnv).
                    docservice = next(doc for doc in documents
                                      if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "docservice")
                    user_env = {entry["name"]: entry.get("value")
                                for env in environment_lists(docservice) for entry in env}
                    secrets = {doc["metadata"]["name"]: doc for doc in documents if doc["kind"] == "Secret"}
                    for env_prefix, prefix, mode, password in (
                        ("REDIS_SERVER", "redis", node_mode, "node-password"),
                        ("REDIS_SENTINEL", "redisSentinel", sentinel_mode, "sentinel-password"),
                    ):
                        enabled = mode != "none" and (prefix == "redis" or topology == "sentinel")
                        if enabled and AUTH_MODES[mode]:
                            self.assertEqual(user_env[env_prefix + "_USER"], AUTH_MODES[mode])
                        else:
                            self.assertNotIn(env_prefix + "_USER", user_env)
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
                values = {
                    "connections": connections,
                    "docservice": {"editorDataStorage": topology},
                    "adminpanel": {"enabled": True},
                }
                if secret and not (node_tls or sentinel_tls):
                    # A CA without TLS is rejected (see RedisConnectionValidation).
                    self.assertNotEqual(render(values).returncode, 0)
                    continue
                documents = self.documents(values)
                enabled_all = bool(secret and (node_tls or sentinel_tls))
                found = set()
                for doc in documents:
                    spec = doc.get("spec", {})
                    spec = spec.get("template", {}).get("spec", spec)
                    for container in spec.get("containers", []):
                        component = container["name"]
                        if component not in ("docservice", "converter", "adminpanel", "test-ds"):
                            continue
                        found.add(component)
                        # The Converter does not use Redis and gets no Redis CA.
                        enabled = enabled_all and component != "converter"
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
                    if container["name"] == "converter":
                        # The Converter does not use Redis: its own override stays, and no Redis CA volume is added.
                        self.assertNotIn("redis-ca-certificates", [v["name"] for v in spec["volumes"]])
                        continue
                    volume = next(v for v in spec["volumes"] if v["name"] == "redis-ca-certificates")
                    self.assertEqual(volume["secret"]["items"], [{"key": "trust.pem", "path": "ca.crt"}])


class RedisUsersInEnv(unittest.TestCase):
    """Redis usernames are env entries of the Pods that use Redis; the shared ConfigMap has none."""

    CASES = {
        "standalone": ({"docservice": {"editorDataStorage": "standalone"},
                        "connections": {"redisUser": "appuser"}},
                       {"REDIS_SERVER_USER": "appuser"}),
        "sentinel": ({"docservice": {"editorDataStorage": "sentinel"},
                      "connections": {"redisSentinelNodes": ["sentinel:26379"], "redisSentinelNoPass": False,
                                      "redisSentinelUser": "sentuser", "redisSentinelPassword": "x"}},
                     {"REDIS_SERVER_USER": "default", "REDIS_SENTINEL_USER": "sentuser"}),
    }

    def test_users_reach_only_the_redis_pods(self):
        for name, (values, expected) in self.CASES.items():
            with self.subTest(case=name):
                result = render({"adminpanel": {"enabled": True}, **values})
                self.assertEqual(result.returncode, 0, result.stderr)
                documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
                config = next(doc for doc in documents if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "documentserver")
                self.assertFalse({"REDIS_SERVER_USER", "REDIS_SENTINEL_USER"} & set(config["data"]))
                seen = set()
                for doc in documents:
                    if doc["kind"] not in ("Deployment", "StatefulSet", "Pod"):
                        continue
                    pod = doc["spec"].get("template", doc)["spec"] if doc["kind"] != "Pod" else doc["spec"]
                    for container in pod["containers"]:
                        label = container["name"]
                        if label not in ("docservice", "adminpanel", "test-ds", "converter"):
                            continue
                        seen.add(label)
                        env = {e["name"]: e.get("value") for e in container.get("env", [])}
                        if label == "converter":
                            self.assertFalse({"REDIS_SERVER_USER", "REDIS_SENTINEL_USER"} & set(env))
                            # No envFrom source may provide them either: only the shared ConfigMap and Secrets.
                            for source in container.get("envFrom", []):
                                if "configMapRef" in source:
                                    self.assertEqual(source["configMapRef"]["name"], "documentserver")
                        else:
                            for key, value in expected.items():
                                self.assertEqual(env.get(key), value, label)
                            if name == "standalone":
                                self.assertNotIn("REDIS_SENTINEL_USER", env)
                self.assertEqual(seen, {"docservice", "adminpanel", "test-ds", "converter"})


class ConverterGetsNoRedis(unittest.TestCase):
    """Converter never uses Redis: no credentials, CA env, volume or mount, whatever the Redis settings."""

    def components(self, values):
        result = render({"adminpanel": {"enabled": True}, **values})
        self.assertEqual(result.returncode, 0, result.stderr)
        found = {}
        for doc in yaml.safe_load_all(result.stdout):
            if doc and doc["kind"] in ("Deployment", "StatefulSet"):
                spec = doc["spec"]["template"]["spec"]
                for container in spec["containers"]:
                    if container["name"] in ("docservice", "converter", "adminpanel"):
                        found[container["name"]] = (container, spec)
        self.assertEqual(set(found), {"docservice", "converter", "adminpanel"})
        return found

    def test_standalone_with_tls_and_ca(self):
        found = self.components({
            "docservice": {"editorDataStorage": "standalone"},
            "connections": {"redisTLS": True, "redisCAExistingSecret": "my-ca", "redisPassword": "pw"},
        })
        redis_env = {"REDIS_SERVER_PWD", "REDIS_SENTINEL_PWD", "NODE_EXTRA_CA_CERTS"}
        for name, (container, spec) in found.items():
            env = {entry["name"] for entry in container.get("env", [])}
            volumes = [v["name"] for v in spec.get("volumes", [])]
            mounts = [m["name"] for m in container.get("volumeMounts", [])]
            if name == "converter":
                self.assertFalse(env & redis_env)
                self.assertNotIn("redis-ca-certificates", volumes)
                self.assertNotIn("redis-ca-certificates", mounts)
                self.assertNotIn("my-ca", json.dumps(spec))
            else:
                self.assertIn("REDIS_SERVER_PWD", env, name)
                self.assertIn("NODE_EXTRA_CA_CERTS", env, name)
                self.assertIn("redis-ca-certificates", volumes, name)
                self.assertIn("redis-ca-certificates", mounts, name)

    def test_converter_keeps_its_own_ca_override(self):
        override = {"name": "NODE_EXTRA_CA_CERTS", "value": "/converter/ca.pem"}
        found = self.components({"converter": {"extraEnvVars": [override]}})
        env = [e for e in found["converter"][0]["env"] if e["name"] == "NODE_EXTRA_CA_CERTS"]
        self.assertEqual(env, [override])

    def test_statistics_in_redis_only_for_adminpanel_and_docservice(self):
        found = self.components({"docservice": {"editorDataStorage": "memory", "editorStatStorage": "standalone"},
                                 "connections": {"redisPassword": "pw"}})
        for name, (container, _) in found.items():
            env = {entry["name"] for entry in container.get("env", [])}
            self.assertEqual("REDIS_SERVER_PWD" in env, name != "converter", name)


class RedisConnectionValidation(unittest.TestCase):
    CLUSTER = {"docservice": {"editorDataStorage": "cluster"}, "connections": {"redisClusterNodes": ["redis:6379"]}}
    SENTINEL = {"docservice": {"editorDataStorage": "sentinel"}, "connections": {"redisSentinelNodes": ["sentinel:26379"]}}
    STANDALONE = {"docservice": {"editorDataStorage": "standalone"}, "connections": {}}

    def values(self, base, **connections):
        return {"docservice": dict(base["docservice"]),
                "connections": {**base["connections"], **connections}}

    def assertFails(self, values, message):
        result = render(values)
        self.assertNotEqual(result.returncode, 0, result.stdout[:200])
        self.assertIn(message, result.stderr)

    def assertRenders(self, values):
        result = render(values)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cluster_database(self):
        self.assertFails(self.values(self.CLUSTER, redisDBNum="3"), "supports only database 0")
        self.assertFails(self.values(self.CLUSTER, redisDBNum=3), "supports only database 0")
        for db in ("0", "00", ""):
            with self.subTest(db=db):
                self.assertRenders(self.values(self.CLUSTER, redisDBNum=db))
        self.assertRenders(self.values(self.CLUSTER))

    def test_database_for_other_topologies(self):
        for base in (self.STANDALONE, self.SENTINEL):
            self.assertRenders(self.values(base, redisDBNum="3"))

    def test_database_must_be_a_non_negative_integer(self):
        for db in ("db1", "-1", "1.5"):
            with self.subTest(db=db):
                self.assertFails(self.values(self.STANDALONE, redisDBNum=db), "non-negative integer")

    def test_ca_requires_tls(self):
        self.assertFails(self.values(self.STANDALONE, redisCAExistingSecret="ca"), "redisCAExistingSecret is set")
        self.assertRenders(self.values(self.STANDALONE, redisCAExistingSecret="ca", redisTLS=True))
        self.assertRenders(self.values(self.SENTINEL, redisCAExistingSecret="ca", redisSentinelTLS=True))

    def test_not_enforced_when_redis_is_not_used(self):
        self.assertRenders({"connections": {"redisCAExistingSecret": "ca", "redisTLS": False}})
        self.assertRenders({"connections": {"redisDBNum": "db1"}})
        self.assertRenders({"connections": {"redisClusterNodes": ["redis:6379"], "redisDBNum": "3"}})


class LegacyConnectorName(unittest.TestCase):
    NOTICE = "connections.redisConnectorName is obsolete since chart 8.0.0"

    def notes(self, values):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as overrides:
            json.dump(values, overrides)
            overrides.flush()
            result = subprocess.run(
                ["helm", "install", "connector-test", str(CHART), "--dry-run=client", "-f", overrides.name],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split("NOTES:", 1)[1]

    def test_other_values_fail(self):
        for name in ("ioredis", "something"):
            with self.subTest(name=name):
                result = render({"connections": {"redisConnectorName": name}})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("was removed in chart 8.0.0", result.stderr)
                self.assertIn("editorDataStorage: sentinel", result.stderr)

    def test_redis_is_ignored_with_notice(self):
        self.assertEqual(render({"connections": {"redisConnectorName": "redis"}}).returncode, 0)
        self.assertIn(self.NOTICE, self.notes({"connections": {"redisConnectorName": "redis"}}))

    def test_absent_empty_and_null_are_silent(self):
        for connections in ({}, {"redisConnectorName": ""}, {"redisConnectorName": None}):
            with self.subTest(connections=connections):
                self.assertEqual(render({"connections": connections}).returncode, 0)
                self.assertNotIn(self.NOTICE, self.notes({"connections": connections}))


class MemoryMultiReplicaWarning(unittest.TestCase):
    """helm template does not render NOTES.txt; helm install --dry-run=client does, without a cluster."""

    WARNING = "WARNING: In-memory editor storage with more than one DocService Pod."

    def notes(self, values):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as overrides:
            json.dump(values, overrides)
            overrides.flush()
            result = subprocess.run(
                ["helm", "install", "notes-test", str(CHART), "--dry-run=client", "-f", overrides.name],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split("NOTES:", 1)[1]

    def test_default_warns(self):
        notes = self.notes({})
        self.assertIn(self.WARNING, notes)
        self.assertIn("runs 2 replicas.", notes)
        self.assertNotIn("with autoscaling", notes)

    def test_single_replica_does_not_warn(self):
        self.assertNotIn(self.WARNING, self.notes({"docservice": {"replicas": 1}}))

    def test_autoscaling_warns(self):
        notes = self.notes({"docservice": {"replicas": 1, "autoscaling": {"enabled": True}}})
        self.assertIn(self.WARNING, notes)
        self.assertIn("with autoscaling", notes)

    def test_redis_data_storage_does_not_warn(self):
        for stat in ("", "memory"):
            with self.subTest(stat=stat):
                notes = self.notes({"docservice": {"editorDataStorage": "standalone", "editorStatStorage": stat,
                                                   "replicas": 2}})
                self.assertNotIn(self.WARNING, notes)

    def test_memory_data_with_redis_statistics_warns(self):
        notes = self.notes({"docservice": {"editorDataStorage": "memory", "editorStatStorage": "standalone",
                                           "replicas": 2}})
        self.assertIn(self.WARNING, notes)


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
                    # The Converter does not use Redis (see ConverterGetsNoRedis).
                    self.assertEqual("REDIS_SERVER_PWD" in self.env_names(doc), name != "converter", name)

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


class StorageModeConsistency(unittest.TestCase):
    """Everything that depends on the editor storage modes agrees with the modes computed from the values."""

    CONNECTIONS = {
        "memory": {},
        "standalone": {"redisPassword": "x"},
        "sentinel": {"redisPassword": "x", "redisSentinelNodes": ["s1:26379"]},
        "cluster": {"redisPassword": "x", "redisClusterNodes": ["c1:6379"]},
    }

    def combinations(self):
        for data in (None, "memory", "standalone", "sentinel", "cluster"):
            for stat in (None, "memory", "same"):
                if stat == "same":
                    if data is None:
                        continue
                    stat = data
                yield data, stat

    def values(self, data, stat):
        effective_data = data or "memory"
        effective_stat = stat or effective_data
        topology = next((m for m in (effective_data, effective_stat) if m != "memory"), "memory")
        docservice = {}
        if data is not None:
            docservice["editorDataStorage"] = data
        if stat is not None:
            docservice["editorStatStorage"] = stat
        return {"docservice": docservice, "connections": self.CONNECTIONS[topology]}, effective_data, effective_stat

    def notes(self, values):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as overrides:
            json.dump(values, overrides)
            overrides.flush()
            result = subprocess.run(
                ["helm", "install", "notes-test", str(CHART), "--dry-run=client", "-f", overrides.name],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_modes_agree_everywhere(self):
        for data, stat in self.combinations():
            with self.subTest(data=data, stat=stat):
                values, effective_data, effective_stat = self.values(data, stat)
                redis = "memory" != effective_data or "memory" != effective_stat
                result = render(values)
                self.assertEqual(result.returncode, 0, result.stderr)
                documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
                config = next(doc["data"] for doc in documents if doc["metadata"]["name"] == "documentserver")
                self.assertEqual(config.get("EDITOR_DATA_STORAGE"), "editorDataRedis" if effective_data != "memory" else None)
                if effective_stat != "memory":
                    expected_stat = "editorDataRedis"
                else:
                    expected_stat = "editorDataMemory" if stat == "memory" else None
                self.assertEqual(config.get("EDITOR_STAT_STORAGE"), expected_stat)
                hook = [doc for doc in documents if doc["kind"] == "Pod" and doc["metadata"]["name"] == "test-redis-config"]
                self.assertEqual(bool(hook), redis)
                docservice = next(doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "docservice")
                names = {e["name"] for env in environment_lists(docservice) for e in env}
                self.assertEqual("REDIS_SERVER_PWD" in names, redis)
                warning = MemoryMultiReplicaWarning.WARNING in self.notes(values)
                self.assertEqual(warning, effective_data == "memory")


class RedisBooleanParsing(unittest.TestCase):
    """The Redis flags are parsed like the DocumentServer entrypoint (build/scripts/lib/bool.sh)."""

    STANDALONE = {"docservice": {"editorDataStorage": "standalone"}, "connections": {"redisPassword": "x"}}
    SENTINEL = {"docservice": {"editorDataStorage": "sentinel"},
                "connections": {"redisPassword": "x", "redisSentinelNodes": ["s1:26379"],
                                "redisSentinelPassword": "y"}}

    def values(self, base, **connections):
        return {"docservice": dict(base["docservice"]), "connections": {**base["connections"], **connections}}

    def documents(self, values):
        result = render(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [doc for doc in yaml.safe_load_all(result.stdout) if doc]

    def docservice(self, documents):
        return next(doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "docservice")

    def env_names(self, documents):
        return {e["name"] for env in environment_lists(self.docservice(documents)) for e in env}

    def test_true_strings(self):
        for text in ("True", "yes", "1", "on", " Y ", "TRUE"):
            with self.subTest(redisTLS=text):
                documents = self.documents(self.values(self.STANDALONE, redisTLS=text, redisCAExistingSecret="ca"))
                config = next(doc["data"] for doc in documents if doc["metadata"]["name"] == "documentserver")
                self.assertEqual(config["REDIS_SERVER_TLS"], "true")
                self.assertNotIn("REDIS_SENTINEL_TLS", config)  # false and no Sentinel nodes: not rendered
                self.assertIn("NODE_EXTRA_CA_CERTS", self.env_names(documents))
                volumes = self.docservice(documents)["spec"]["template"]["spec"]["volumes"]
                self.assertIn("redis-ca-certificates", [v["name"] for v in volumes])

    def test_false_strings_do_not_enable_tls(self):
        for text in ("false", "No", "0", "off", " N ", ""):
            with self.subTest(redisTLS=text):
                result = render(self.values(self.STANDALONE, redisTLS=text, redisCAExistingSecret="ca"))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("redisCAExistingSecret is set", result.stderr)
                documents = self.documents(self.values(self.STANDALONE, redisTLS=text))
                config = next(doc["data"] for doc in documents if doc["metadata"]["name"] == "documentserver")
                self.assertEqual(config["REDIS_SERVER_TLS"], "false")

    def test_unrecognised_values_fail(self):
        for name, base in (("redisTLS", self.STANDALONE), ("redisSentinelTLS", self.STANDALONE),
                           ("redisNoPass", self.STANDALONE), ("redisSentinelNoPass", self.SENTINEL)):
            with self.subTest(flag=name):
                result = render(self.values(base, **{name: "maybe"}))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("connections.%s must be a boolean (true/false), got \"maybe\"." % name, result.stderr)

    def test_no_pass_strings(self):
        for text, present in (("false", True), ("False", True), ("true", False), ("YES", False), (True, False), (False, True)):
            with self.subTest(redisNoPass=text):
                values = self.values(self.STANDALONE, redisNoPass=text)
                if present is False:
                    del values["connections"]["redisPassword"]
                names = self.env_names(self.documents(values))
                self.assertEqual("REDIS_SERVER_PWD" in names, present)

    def test_sentinel_no_pass_strings(self):
        for text, present in (("false", True), ("Off", True), ("true", False), ("1", False), (False, True)):
            with self.subTest(redisSentinelNoPass=text):
                names = self.env_names(self.documents(self.values(self.SENTINEL, redisSentinelNoPass=text)))
                self.assertEqual("REDIS_SENTINEL_PWD" in names, present)


class UnusedRedisConfigMapKeys(unittest.TestCase):
    """REDIS_CONNECTOR_NAME is never rendered; REDIS_SENTINEL_TLS only for Sentinel nodes or a true flag."""

    TOPOLOGIES = {
        "memory": {"docservice": {}, "connections": {}},
        "standalone": {"docservice": {"editorDataStorage": "standalone"}, "connections": {"redisPassword": "x"}},
        "cluster": {"docservice": {"editorDataStorage": "cluster"},
                    "connections": {"redisPassword": "x", "redisClusterNodes": ["c1:6379"]}},
        "sentinel": {"docservice": {"editorDataStorage": "sentinel"},
                     "connections": {"redisPassword": "x", "redisSentinelNodes": ["s1:26379"]}},
    }

    def config(self, topology, **connections):
        base = self.TOPOLOGIES[topology]
        result = render({"docservice": base["docservice"], "connections": {**base["connections"], **connections}})
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
        return next(doc["data"] for doc in documents if doc["metadata"]["name"] == "documentserver")

    def test_connector_name_is_never_rendered(self):
        for topology in self.TOPOLOGIES:
            for flag in (False, True):
                with self.subTest(topology=topology, redisSentinelTLS=flag):
                    self.assertNotIn("REDIS_CONNECTOR_NAME", self.config(topology, redisSentinelTLS=flag))

    def test_sentinel_tls_absent_when_false_without_sentinel_nodes(self):
        for topology in ("memory", "standalone", "cluster"):
            with self.subTest(topology=topology):
                self.assertNotIn("REDIS_SENTINEL_TLS", self.config(topology))
                self.assertNotIn("REDIS_SENTINEL_TLS", self.config(topology, redisSentinelTLS="false"))

    def test_sentinel_tls_true_is_kept_in_any_mode(self):
        # The entrypoint also applies it to a Sentinel configured in local.json or NODE_CONFIG.
        for topology in self.TOPOLOGIES:
            for value in (True, "true", "Yes"):
                with self.subTest(topology=topology, redisSentinelTLS=value):
                    self.assertEqual(self.config(topology, redisSentinelTLS=value)["REDIS_SENTINEL_TLS"], "true")

    def test_sentinel_mode_renders_both_values(self):
        self.assertEqual(self.config("sentinel")["REDIS_SENTINEL_TLS"], "false")
        self.assertEqual(self.config("sentinel", redisSentinelTLS="off")["REDIS_SENTINEL_TLS"], "false")
        self.assertEqual(self.config("sentinel", redisSentinelTLS=True)["REDIS_SENTINEL_TLS"], "true")


class RedisTestHookNotes(unittest.TestCase):
    """NOTES.txt mentions the test-redis-config hook only when the hook is rendered."""

    def notes(self, values, suffix=None):
        values = {**values, **({"commonNameSuffix": suffix} if suffix else {})}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as overrides:
            json.dump(values, overrides)
            overrides.flush()
            result = subprocess.run(
                ["helm", "install", "notes-test", str(CHART), "--dry-run=client", "-f", overrides.name],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split("NOTES:", 1)[1]

    def test_redis_modes_mention_the_hook(self):
        for mode, connections in (("standalone", {"redisPassword": "x"}),
                                  ("sentinel", {"redisPassword": "x", "redisSentinelNodes": ["s:26379"]}),
                                  ("cluster", {"redisPassword": "x", "redisClusterNodes": ["c:6379"]})):
            with self.subTest(mode=mode):
                notes = self.notes({"docservice": {"editorDataStorage": mode}, "connections": connections})
                self.assertIn("kubectl logs -f test-redis-config --namespace=", notes)
                self.assertIn("kubectl logs -f test-ds --namespace=", notes)

    def test_statistics_only_redis_mentions_the_hook(self):
        notes = self.notes({"docservice": {"editorStatStorage": "standalone"}, "connections": {"redisPassword": "x"}})
        self.assertIn("test-redis-config", notes)

    def test_name_suffix_is_used(self):
        notes = self.notes({"docservice": {"editorDataStorage": "standalone"}, "connections": {"redisPassword": "x"}},
                           suffix="-sfx")
        self.assertRegex(notes, r"kubectl logs -f test-redis-config\S* --namespace=")

    def test_memory_mode_does_not_mention_it(self):
        notes = self.notes({})
        self.assertIn("kubectl logs -f test-ds --namespace=", notes)
        self.assertNotIn("test-redis-config", notes)

    def test_disabled_tests_do_not_mention_it(self):
        notes = self.notes({"tests": {"enabled": False}, "docservice": {"editorDataStorage": "standalone"},
                            "connections": {"redisPassword": "x"}})
        self.assertNotIn("test-redis-config", notes)
        self.assertNotIn("Helm test", notes)


if __name__ == "__main__":
    unittest.main()
