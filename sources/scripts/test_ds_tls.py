#!/usr/bin/env python3
"""Connectivity hook TLS regression tests. Run with Python 3 and redis-py:

    python3 sources/scripts/test_ds_tls.py
"""

import importlib.util
import itertools
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

import redis
import redis.cluster


SCRIPT = Path(__file__).with_name("test_ds.py")


def load_hook(env):
    spec = importlib.util.spec_from_file_location("connectivity_hook", SCRIPT)
    hook = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"DB_PORT": "5432", **env}, clear=True):
        spec.loader.exec_module(hook)
        hook.logger_test_ds = Mock()
        return hook


class RedisUsage(unittest.TestCase):
    def test_redis_in_use(self):
        cases = (
            ({"EDITOR_DATA_STORAGE": "editorDataMemory", "EDITOR_STAT_STORAGE": "editorDataMemory"}, False),
            ({"EDITOR_DATA_STORAGE": "editorDataRedis", "EDITOR_STAT_STORAGE": "editorDataMemory"}, True),
            ({"EDITOR_DATA_STORAGE": "editorDataMemory", "EDITOR_STAT_STORAGE": "editorDataRedis"}, True),
            ({"EDITOR_DATA_STORAGE": "editorDataRedis", "EDITOR_STAT_STORAGE": "editorDataRedis"}, True),
            ({}, False),
        )
        for env, expected in cases:
            with self.subTest(env=env):
                hook = load_hook(env)
                with patch.dict(os.environ, env, clear=True):
                    self.assertIs(hook.redis_in_use(), expected)


class TotalStatus(unittest.TestCase):
    def status(self, results):
        hook = load_hook({})
        hook.total_result.clear()
        hook.total_result.update(results)
        try:
            hook.total_status()
        except SystemExit as exit_info:
            return exit_info.code
        return None

    def test_success_and_skipped_pass(self):
        self.assertIsNone(self.status({"CheckRedis": "Skipped", "CheckDS": "Success", "CheckDir": "Success"}))

    def test_failed_fails(self):
        self.assertEqual(self.status({"CheckRedis": "Skipped", "CheckDS": "Failed"}), 1)
        self.assertEqual(self.status({"CheckRedis": "Failed", "CheckDS": "Success"}), 1)

    def test_unknown_value_fails(self):
        self.assertEqual(self.status({"CheckRedis": "Unknown", "CheckDS": "Success"}), 1)


class RedisKey(unittest.TestCase):
    def check(self, stored):
        hook = load_hook({})
        hook.total_result.clear()
        hook.rc = MagicMock()
        hook.rc.get.return_value = stored
        hook.check_redis_key()
        return hook

    def test_key_prefix_expiry_and_delete(self):
        hook = self.check(b"ok")
        key = hook.rc.set.call_args.args[0]
        self.assertTrue(key.startswith("ds:helm-test:"))
        self.assertEqual(hook.rc.set.call_args.kwargs["ex"], 60)
        hook.rc.delete.assert_called_once_with(key)
        self.assertEqual(hook.total_result["CheckRedis"], "Success")

    def test_mismatch_fails(self):
        hook = self.check(b"other")
        self.assertEqual(hook.total_result["CheckRedis"], "Failed")


class RedisTLSConnectivity(unittest.TestCase):
    def test_tls_and_ca_settings_reach_each_client(self):
        for topology, node_tls, sentinel_tls, node_ca, sentinel_ca in itertools.product(
            ("standalone", "cluster", "sentinel"), (False, True), (False, True),
            ("", "/ca/nodes.pem"), ("", "/ca/sentinels.pem"),
        ):
            with self.subTest(topology=topology, node_tls=node_tls, sentinel_tls=sentinel_tls,
                              node_ca=node_ca, sentinel_ca=sentinel_ca):
                env = {
                    "DB_PORT": "5432",
                    "REDIS_SERVER_HOST": "redis", "REDIS_SERVER_PORT": "6379",
                    "REDIS_SERVER_TLS": str(node_tls).lower(),
                    "REDIS_SENTINEL_TLS": str(sentinel_tls).lower(),
                    "REDIS_SERVER_CA_CERTS": node_ca,
                    "REDIS_SENTINEL_CA_CERTS": sentinel_ca,
                }
                if topology == "cluster":
                    env["REDIS_CLUSTER_NODES"] = "redis:6379"
                elif topology == "sentinel":
                    env.update({"REDIS_SENTINEL_NODES": "sentinel:26379",
                                "REDIS_SENTINEL_GROUP_NAME": "mymaster"})
                spec = importlib.util.spec_from_file_location("connectivity_hook", SCRIPT)
                hook = importlib.util.module_from_spec(spec)
                with patch.dict(os.environ, env, clear=True):
                    spec.loader.exec_module(hook)
                hook.logger_test_ds = Mock()
                with patch.object(redis, "Redis") as standalone, \
                        patch.object(redis.cluster, "RedisCluster") as cluster, \
                        patch.object(redis, "Sentinel") as sentinel:
                    sentinel.return_value.discover_master.return_value = ("master", 6379)
                    client = cluster if topology == "cluster" else standalone
                    client.return_value.ping.return_value = True
                    check = {"standalone": hook.get_redis_status,
                             "cluster": hook.get_redis_cluster_status,
                             "sentinel": hook.get_redis_sentinel_status}[topology]
                    self.assertIs(check(), True)
                    self.assert_tls_options(client.call_args.kwargs, node_tls, node_ca)
                    if topology == "sentinel":
                        self.assert_tls_options(sentinel.call_args.kwargs["sentinel_kwargs"],
                                                sentinel_tls, sentinel_ca)
                        self.assertEqual(client.call_args.kwargs["host"], "master")

    def assert_tls_options(self, options, enabled, ca):
        if enabled:
            self.assertIs(options["ssl"], True)
            self.assertEqual(options["ssl_cert_reqs"], "required")
            if ca:
                self.assertEqual(options["ssl_ca_certs"], ca)
            else:
                self.assertNotIn("ssl_ca_certs", options)
        else:
            self.assertNotIn("ssl", options)
            self.assertNotIn("ssl_cert_reqs", options)
            self.assertNotIn("ssl_ca_certs", options)


class RedisAllNodes(unittest.TestCase):
    """The Sentinel and Cluster clients get every configured node, in order, and the other options are unchanged."""

    BASE = {
        "DB_PORT": "5432", "REDIS_SERVER_HOST": "redis", "REDIS_SERVER_PORT": "6379",
        "REDIS_SERVER_USER": "app", "REDIS_SERVER_PWD": "pw",
        "REDIS_SERVER_TLS": "true", "REDIS_SERVER_CA_CERTS": "/ca/nodes.pem",
        "REDIS_SENTINEL_TLS": "true", "REDIS_SENTINEL_CA_CERTS": "/ca/sentinels.pem",
        "REDIS_SENTINEL_USER": "su", "REDIS_SENTINEL_PWD": "sp",
    }

    def load(self, env):
        spec = importlib.util.spec_from_file_location("connectivity_hook", SCRIPT)
        hook = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {**self.BASE, **env}, clear=True):
            spec.loader.exec_module(hook)
        hook.logger_test_ds = Mock()
        return hook

    def test_parse_nodes(self):
        hook = self.load({})
        self.assertEqual(hook.parse_nodes("a:1 b.example:2  c:3"), [("a", 1), ("b.example", 2), ("c", 3)])
        self.assertEqual(hook.parse_nodes("[::1]:26379 rediss://h:6380"), [("::1", 26379), ("h", 6380)])
        self.assertEqual(hook.parse_nodes(""), [])

    def test_sentinel_gets_all_nodes(self):
        hook = self.load({"REDIS_SENTINEL_NODES": "s0:26379 s1:26380 s2:26381", "REDIS_SENTINEL_GROUP_NAME": "mymaster"})
        with patch.object(redis, "Redis") as client, patch.object(redis, "Sentinel") as sentinel:
            sentinel.return_value.discover_master.return_value = ("master", 6379)
            client.return_value.ping.return_value = True
            self.assertIs(hook.get_redis_sentinel_status(), True)
        self.assertEqual(sentinel.call_args.args[0], [("s0", 26379), ("s1", 26380), ("s2", 26381)])
        kwargs = sentinel.call_args.kwargs["sentinel_kwargs"]
        self.assertEqual((kwargs["username"], kwargs["password"], kwargs["ssl"], kwargs["ssl_ca_certs"]),
                         ("su", "sp", True, "/ca/sentinels.pem"))
        sentinel.return_value.discover_master.assert_called_once_with("mymaster")
        data = client.call_args.kwargs
        self.assertEqual((data["host"], data["username"], data["password"], data["ssl_ca_certs"]),
                         ("master", "app", "pw", "/ca/nodes.pem"))

    def test_cluster_gets_all_startup_nodes(self):
        hook = self.load({"REDIS_CLUSTER_NODES": "c0:6379 c1:6380 c2:6381"})
        with patch.object(redis.cluster, "RedisCluster") as cluster:
            cluster.return_value.ping.return_value = True
            self.assertIs(hook.get_redis_cluster_status(), True)
        nodes = cluster.call_args.kwargs["startup_nodes"]
        self.assertEqual([(n.host, n.port) for n in nodes], [("c0", 6379), ("c1", 6380), ("c2", 6381)])
        kwargs = cluster.call_args.kwargs
        self.assertEqual((kwargs["username"], kwargs["password"], kwargs["ssl"], kwargs["ssl_ca_certs"]),
                         ("app", "pw", True, "/ca/nodes.pem"))


class RabbitMQQueue(unittest.TestCase):
    def test_test_queue_is_exclusive(self):
        """RabbitMQ 4.x denies transient non-exclusive queues by default (transient_nonexcl_queues)."""
        hook = load_hook({})
        hook.total_result.clear()
        pika = MagicMock()
        pika.BlockingConnection.return_value.is_open = True
        channel = pika.BlockingConnection.return_value.channel.return_value
        with patch.dict(sys.modules, {"pika": pika}):
            hook.check_mq_rabbitmq()
        channel.queue_declare.assert_called_once_with(queue="testDocsServer", exclusive=True)
        channel.queue_delete.assert_called_once_with(queue="testDocsServer")
        self.assertEqual(hook.total_result["CheckRabbitMQ"], "Success")

    def test_declare_error_fails_the_check(self):
        hook = load_hook({})
        hook.total_result.clear()
        pika = MagicMock()
        pika.BlockingConnection.return_value.is_open = True
        pika.BlockingConnection.return_value.channel.return_value.queue_declare.side_effect = Exception("denied")
        with patch.dict(sys.modules, {"pika": pika}):
            hook.check_mq_rabbitmq()
        self.assertEqual(hook.total_result["CheckRabbitMQ"], "Failed")


if __name__ == "__main__":
    unittest.main()
