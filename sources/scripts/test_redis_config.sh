#!/usr/bin/env bash
# Checks that this DocService image, given the release's environment, selects
# the Redis storage and topology requested by the chart. Connectivity, auth
# and TLS are verified at runtime by the test-ds hook.
set -euo pipefail
fail() { echo "Redis configuration check failed: $*" >&2; exit 1; }
ep=/usr/local/bin/docker-entrypoint.sh

# 1) Capability guard. Images whose entrypoint predates config-only output
#    (e.g. v9.3.8) also ignore EDITOR_DATA_STORAGE and silently run in memory.
grep -q ENTRYPOINT_CONFIG_ONLY_OUTPUT "$ep" ||
  fail "image does not support Redis editor storage; EDITOR_DATA_STORAGE would be ignored"

# 2) Dry run into a private, new file. NODE_CONFIG holds secrets: never print it.
dir=$(mktemp -d); trap 'rm -rf "$dir"' EXIT
ENTRYPOINT_CONFIG_ONLY=true ENTRYPOINT_CONFIG_ONLY_OUTPUT="$dir/node-config.json" \
  timeout 60 "$ep" docservice >/dev/null || fail "entrypoint dry run failed"

# 3) Assert intent only, not the image's internal option layout.
jq -e \
  --arg data     "${EDITOR_DATA_STORAGE:-editorDataMemory}" \
  --arg stat     "${EDITOR_STAT_STORAGE:-${EDITOR_DATA_STORAGE:-editorDataMemory}}" \
  --arg sentinel "${REDIS_SENTINEL_NODES:-}" --arg group "${REDIS_SENTINEL_GROUP_NAME:-}" \
  --arg cluster  "${REDIS_CLUSTER_NODES:-}" '
  .services.CoAuthoring as $c |
  ($c.server.editorDataStorage // "editorDataMemory") == $data and
  ($c.server.editorStatStorage // $data) == $stat and
  if $sentinel != "" then
    $c.redis.optionsSentinel.name == $group and
    [$c.redis.optionsSentinel.sentinelRootNodes[] | "\(.host):\(.port)"] == ($sentinel | split(" "))
  elif $cluster != "" then
    [$c.redis.optionsCluster.rootNodes[].url | sub("^rediss?://"; "")] == ($cluster | split(" "))
  else
    (($c.redis.optionsSentinel // {}) | length) == 0 and
    (($c.redis.optionsCluster  // {}) | length) == 0
  end' "$dir/node-config.json" >/dev/null ||
  fail "generated configuration does not match the requested storage/topology"

echo "Redis configuration matches the requested storage and topology"
