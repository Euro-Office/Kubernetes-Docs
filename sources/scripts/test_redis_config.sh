#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "Redis configuration test failed: $*" >&2
  exit 1
}

command -v jq >/dev/null 2>&1 || fail "jq is not available in the DocumentServer image"
[[ -x /usr/local/bin/docker-entrypoint.sh ]] || fail "the native DocumentServer entrypoint is not available"

[[ -n "${REDIS_SERVER_DB_NUM:-}" ]] || fail "REDIS_SERVER_DB_NUM is not set"

if [[ -n "${REDIS_SENTINEL_NODES:-}" && -n "${REDIS_CLUSTER_NODES:-}" ]]; then
  fail "Redis Sentinel and Cluster nodes cannot both be set"
elif [[ -n "${REDIS_SENTINEL_NODES:-}" ]]; then
  topology=sentinel
  [[ -n "${REDIS_SENTINEL_GROUP_NAME:-}" ]] || fail "REDIS_SENTINEL_GROUP_NAME is not set"
elif [[ -n "${REDIS_CLUSTER_NODES:-}" ]]; then
  topology=cluster
else
  topology=standalone
fi

# The orchestrated entrypoint is the component that turns Helm's environment
# into NODE_CONFIG. ENTRYPOINT_CONFIG_ONLY prevents DocService from starting;
# the regular Helm test pod separately checks that DocService starts and
# connects successfully.
render_config() {
  # Explicitly remove the obsolete aliases while exercising the native names.
  env -u REDIS_SERVER_PASS -u REDIS_SENTINEL_PASS -u REDIS_SERVER_DB \
    "$@" /usr/local/bin/docker-entrypoint.sh docservice
}

# Validate the username for this case, which may override the deployed one.
# An omitted username selects the default account through password-only AUTH.
check_auth() {
  local name="$1" options="$2" authenticated="$3" username="$4" user_key="${5:-username}"
  printf '%s' "$options" | jq -e \
    --argjson authenticated "$authenticated" --arg username "$username" --arg user_key "$user_key" '
    if $authenticated then
      (.password | type == "string" and length > 0) and
      (if $username != "" then .[$user_key] == $username else has($user_key) | not end)
    else
      (has($user_key) or has("password")) | not
    end
  ' >/dev/null || fail "$name: credentials do not match the expected authentication"
}

check_config() {
  local name="$1"
  local node_config="$2"
  local node_auth="$3"
  local sentinel_auth="$4"
  local node_user="${5-${REDIS_SERVER_USER:-}}"
  local sentinel_user="${6-${REDIS_SENTINEL_USER:-}}"
  local node_tls="${7:-${REDIS_SERVER_TLS:-false}}"
  local sentinel_tls="${8:-${REDIS_SENTINEL_TLS:-false}}"
  local scheme=redis
  [[ "$node_tls" != true ]] || scheme=rediss

  printf '%s' "$node_config" | jq -e --argjson tls "$( [[ "$node_tls" == true ]] && echo true || echo false )" '
  .services.CoAuthoring.redis.options.socket |
  (if $tls then .tls == true else has("tls") | not end) and
  .rejectUnauthorized != false
  ' >/dev/null || fail "$name: standalone/fallback TLS options are incorrect"

  printf '%s' "$node_config" | jq -e \
    --arg data_storage "${EDITOR_DATA_STORAGE:-editorDataMemory}" \
    --arg stat_storage "${EDITOR_STAT_STORAGE:-${EDITOR_DATA_STORAGE:-editorDataMemory}}" '
  (.services.CoAuthoring.server.editorDataStorage // "editorDataMemory") == $data_storage and
  (.services.CoAuthoring.server.editorStatStorage // $data_storage) == $stat_storage and
  .services.CoAuthoring.redis.name == "redis"
' >/dev/null || fail "$name: NODE_CONFIG does not select the native Redis backend"

  # The orchestrated entrypoint uses user/db for standalone options, and
  # username/database for Sentinel and Cluster node-redis options.
  check_auth "$name: standalone/fallback" \
    "$(printf '%s' "$node_config" | jq '.services.CoAuthoring.redis.options')" "$node_auth" "$node_user" user
  printf '%s' "$node_config" | jq -e --arg db "$REDIS_SERVER_DB_NUM" '
    .services.CoAuthoring.redis.options.db == $db
  ' >/dev/null || fail "$name: standalone/fallback database is incorrect"

  if [[ "$topology" == sentinel ]]; then
    printf '%s' "$node_config" | jq -e --arg nodes "$REDIS_SENTINEL_NODES" --arg db "$REDIS_SERVER_DB_NUM" '
    (.services.CoAuthoring.redis.optionsSentinel | type) == "object" and
    (.services.CoAuthoring.redis.optionsSentinel.sentinelRootNodes |
      map("\(.host):\(.port)") == ($nodes | split(" "))) and
    (.services.CoAuthoring.redis.optionsSentinel.nodeClientOptions.database == ($db | tonumber)) and
    ((.services.CoAuthoring.redis.optionsCluster.rootNodes // []) | length) == 0
    ' >/dev/null || fail "$name: NODE_CONFIG does not contain the expected native Sentinel configuration"

    printf '%s' "$node_config" | jq -e \
      --argjson node_tls "$( [[ "$node_tls" == true ]] && echo true || echo false )" \
      --argjson sentinel_tls "$( [[ "$sentinel_tls" == true ]] && echo true || echo false )" '
    def check_tls($enabled):
      .socket | (if $enabled then .tls == true else has("tls") | not end) and
      .rejectUnauthorized != false;
    .services.CoAuthoring.redis.optionsSentinel |
    (.nodeClientOptions | check_tls($node_tls)) and
    (.sentinelClientOptions | check_tls($sentinel_tls))
    ' >/dev/null || fail "$name: Redis node and Sentinel TLS options are incorrect"

    check_auth "$name: Redis nodes" \
      "$(printf '%s' "$node_config" | jq '.services.CoAuthoring.redis.optionsSentinel.nodeClientOptions')" "$node_auth" "$node_user"
    check_auth "$name: Sentinel" \
      "$(printf '%s' "$node_config" | jq '.services.CoAuthoring.redis.optionsSentinel.sentinelClientOptions')" "$sentinel_auth" "$sentinel_user"
    if [[ "$sentinel_auth" != true ]]; then
      printf '%s' "$node_config" | jq -e '
      (.services.CoAuthoring.redis.optionsSentinel.sentinelClientOptions | del(.socket)) == {}
      ' >/dev/null || fail "$name: unauthenticated Sentinel options are not empty"
    fi
  elif [[ "$topology" == cluster ]]; then
    printf '%s' "$node_config" | jq -e --arg nodes "$REDIS_CLUSTER_NODES" --arg scheme "$scheme" '
    (.services.CoAuthoring.redis.optionsCluster.rootNodes |
      map(.url) == ($nodes | split(" ") | map($scheme + "://" + .))) and
    ((.services.CoAuthoring.redis.optionsSentinel // {}) | length) == 0
    ' >/dev/null || fail "$name: NODE_CONFIG does not contain the expected native Cluster configuration"

    printf '%s' "$node_config" | jq -e --argjson tls "$( [[ "$node_tls" == true ]] && echo true || echo false )" '
    .services.CoAuthoring.redis.optionsCluster.defaults.socket |
    (if $tls then .tls == true else has("tls") | not end) and
    .rejectUnauthorized != false
    ' >/dev/null || fail "$name: Cluster TLS options are incorrect"

    check_auth "$name: Cluster" \
      "$(printf '%s' "$node_config" | jq '.services.CoAuthoring.redis.optionsCluster.defaults')" "$node_auth" "$node_user"
  else
    printf '%s' "$node_config" | jq -e '
      ((.services.CoAuthoring.redis.optionsSentinel // {}) | length) == 0 and
      ((.services.CoAuthoring.redis.optionsCluster // {}) | length) == 0
    ' >/dev/null || fail "$name: standalone configuration contains a topology"
  fi
}

node_config=$(render_config)
check_config "deployed environment" "$node_config" \
  "$([[ -n "${REDIS_SERVER_PWD:-}" ]] && echo true || echo false)" \
  "$([[ -n "${REDIS_SENTINEL_PWD:-}" ]] && echo true || echo false)"

# Exercise the supported authentication matrix using the native environment
# names. These checks only render NODE_CONFIG; the regular test-ds hook checks
# the actual Redis/Sentinel and DocService connections.
for node_auth in none password acl; do
  node_user='' node_password=''
  [[ "$node_auth" == none ]] || node_password=redis-test
  [[ "$node_auth" != acl ]] || node_user=redis-test-user
  sentinel_cases=(none)
  [[ "$topology" != sentinel ]] || sentinel_cases+=(password acl)
  for sentinel_auth in "${sentinel_cases[@]}"; do
    sentinel_user='' sentinel_password=''
    [[ "$sentinel_auth" == none ]] || sentinel_password=sentinel-test
    [[ "$sentinel_auth" != acl ]] || sentinel_user=sentinel-test-user
    check_config "Redis auth=$node_auth, Sentinel auth=$sentinel_auth" \
      "$(render_config "REDIS_SERVER_USER=$node_user" "REDIS_SERVER_PWD=$node_password" \
        "REDIS_SENTINEL_USER=$sentinel_user" "REDIS_SENTINEL_PWD=$sentinel_password")" \
      "$([[ "$node_auth" != none ]] && echo true || echo false)" \
      "$([[ "$sentinel_auth" != none ]] && echo true || echo false)" "$node_user" "$sentinel_user"
  done
done

# Exercise TLS independently, including the entrypoint's empty/unset defaults.
# Authentication is omitted here so TLS-only Sentinel options are also checked.
for node_tls in true false empty unset; do
  for sentinel_tls in true false empty unset; do
    tls_env=(-u REDIS_SERVER_TLS -u REDIS_SENTINEL_TLS)
    [[ "$node_tls" == unset ]] || tls_env+=("REDIS_SERVER_TLS=${node_tls/empty/}")
    [[ "$sentinel_tls" == unset ]] || tls_env+=("REDIS_SENTINEL_TLS=${sentinel_tls/empty/}")
    check_config "Redis TLS=$node_tls, Sentinel TLS=$sentinel_tls" \
      "$(render_config "${tls_env[@]}" REDIS_SERVER_USER= REDIS_SERVER_PWD= REDIS_SENTINEL_USER= REDIS_SENTINEL_PWD=)" \
      false false "" "" "$node_tls" "$sentinel_tls"
  done
done

echo "Redis native $topology configuration is valid"
