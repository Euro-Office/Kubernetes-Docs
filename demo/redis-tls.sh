#!/usr/bin/env sh
# SPDX-FileCopyrightText: 2026 Euro-Office contributors
# SPDX-License-Identifier: AGPL-3.0-only
#
# Creates a test CA and one server certificate for the demo Redis Sentinel and
# Redis Cluster installs (redis-sentinel.yaml, redis-cluster.yaml), and stores them
# in two Secrets of the namespace:
#   redis-tls  tls.crt, tls.key, ca.crt  mounted by the Redis/Sentinel pods (/tls)
#   redis-ca   ca.crt                    for the chart (connections.redisCAExistingSecret)
# It follows develop/redis-tls.sh of Euro-Office/DocumentServer, with the
# Kubernetes service names as subject alternative names. For testing only: with
# cert-manager, issue one Certificate with the same DNS names instead and point
# the Secret names above at it (keys tls.crt, tls.key, ca.crt).
#
# Usage:  sh redis-tls.sh [namespace]            (default: euro-office)
#         REDIS_TLS_OUT=<dir> sh redis-tls.sh [namespace]
#                                                only writes tls.crt, tls.key and
#                                                ca.crt into <dir>, no kubectl
set -eu

namespace=${1:-euro-office}
out_dir=${REDIS_TLS_OUT:-}

umask 077
temp_dir=$(mktemp -d)
# The CA private key never leaves this directory and is removed on exit.
trap 'rm -rf "$temp_dir"' EXIT HUP INT TERM

openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 365 \
  -subj '/CN=Euro-Office Redis test CA' \
  -keyout "$temp_dir/ca.key" -out "$temp_dir/ca.crt" 2>/dev/null
openssl req -new -newkey rsa:2048 -nodes -sha256 \
  -subj '/CN=redis' \
  -keyout "$temp_dir/tls.key" -out "$temp_dir/tls.csr" 2>/dev/null

# The pods are TLS servers and also TLS clients (replication, Sentinel, cluster bus),
# so the certificate allows both uses. One wildcard per headless Service covers
# the pod names (redis-node-0.redis-node.<ns>.svc.cluster.local, ...).
cat > "$temp_dir/extensions.cnf" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=@hosts
[hosts]
DNS.1=*.redis-node.$namespace.svc.cluster.local
DNS.2=redis-node.$namespace.svc.cluster.local
DNS.3=redis-node
DNS.4=*.redis-sentinel.$namespace.svc.cluster.local
DNS.5=redis-sentinel.$namespace.svc.cluster.local
DNS.6=redis-sentinel
DNS.7=*.redis-cluster.$namespace.svc.cluster.local
DNS.8=redis-cluster.$namespace.svc.cluster.local
DNS.9=redis-cluster
DNS.10=localhost
IP.1=127.0.0.1
IP.2=::1
EOF

openssl x509 -req -in "$temp_dir/tls.csr" -CA "$temp_dir/ca.crt" \
  -CAkey "$temp_dir/ca.key" -CAcreateserial -days 365 -sha256 \
  -extfile "$temp_dir/extensions.cnf" -out "$temp_dir/tls.crt" 2>/dev/null

if [ -n "$out_dir" ]; then
  mkdir -p "$out_dir"
  # Test credentials: readable by the container user of a local test.
  chmod 755 "$out_dir"
  for file in tls.crt tls.key ca.crt; do
    cp "$temp_dir/$file" "$out_dir/$file"
    chmod 644 "$out_dir/$file"
  done
  echo "Wrote tls.crt, tls.key and ca.crt to $out_dir"
  exit 0
fi

# Create or update the Secrets (idempotent).
kubectl create secret generic redis-tls --namespace "$namespace" \
  --from-file=tls.crt="$temp_dir/tls.crt" \
  --from-file=tls.key="$temp_dir/tls.key" \
  --from-file=ca.crt="$temp_dir/ca.crt" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic redis-ca --namespace "$namespace" \
  --from-file=ca.crt="$temp_dir/ca.crt" \
  --dry-run=client -o yaml | kubectl apply -f -

cat <<EOF

Secrets redis-tls and redis-ca are ready in namespace $namespace (valid 365 days).
Next steps:
  1. Restart the Redis pods so that they start in TLS mode:
       Sentinel: kubectl rollout restart statefulset/redis-node statefulset/redis-sentinel -n $namespace
       Cluster:  kubectl rollout restart statefulset/redis-cluster -n $namespace
                 kubectl delete job/redis-cluster-init -n $namespace --ignore-not-found
                 kubectl apply -f redis-cluster.yaml     # creates the cluster again
  2. Install or upgrade the chart with the TLS overlay after the topology overlay:
       helm upgrade --install docs .. -n $namespace -f my-values.yml \\
         -f my-values.redis-sentinel.yml -f my-values.redis-tls.yml
     (or my-values.redis-cluster.yml instead of the Sentinel overlay)
To go back to plain Redis: kubectl delete secret redis-tls redis-ca -n $namespace,
restart the StatefulSets as above and install without my-values.redis-tls.yml.
EOF
