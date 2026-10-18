#!/usr/bin/env python3
"""Connectivity hook TLS regression tests. Run with Python 3 and redis-py:

    python3 sources/scripts/test_ds_tls.py
"""

import importlib.util
import itertools
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import redis
import redis.cluster


SCRIPT = Path(__file__).with_name("test_ds.py")


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


if __name__ == "__main__":
    unittest.main()
