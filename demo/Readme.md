# Local Demo Server

This guide walks you through running a local demo of the clustered Euro-Office Document Server using k3d (Kubernetes in Docker). It's intended for development and testing, not production.

## Prerequisites

You need Docker, k3d, kubectl, and Helm installed.

### Docker

```bash
# Ubuntu 24.04
sudo apt update
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu noble stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
newgrp docker
```

### k3d, kubectl, Helm

```bash
# k3d
curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash

# kubectl
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
rm kubectl

# Helm
curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3
chmod 700 get_helm.sh
./get_helm.sh
rm get_helm.sh
```

Verify everything is installed:

```bash
docker --version && k3d version && kubectl version --client && helm version
```

### Resource requirements

Docker Desktop / Colima needs at least **6 GB RAM and 4 CPUs** allocated. The full stack (3 databases + docservice + converter + example + NFS) is not lightweight.

## Build the images

This guide assumes you've already built the cluster images from https://github.com/Euro-Office/DocumentServer:

- `ghcr.io/euro-office/cluster-docs:latest`
- `ghcr.io/euro-office/cluster-utils:latest`
- `ghcr.io/euro-office/cluster-example:latest`


## Set up the cluster

```bash
# Shared directory mounted into all k3d nodes — this is how pods on different
# nodes get RWX storage without NFS in k3d
sudo mkdir -p /tmp/euro-office-shared/{ds-files,ds-runtime-config}
sudo chmod -R 777 /tmp/euro-office-shared

# Create the cluster
k3d cluster create euro-office \
  --servers 1 --agents 2 \
  --port "8080:80@loadbalancer" \
  --k3s-arg "--disable=traefik@server:*" \
  --volume "/tmp/euro-office-shared:/tmp/euro-office-shared@all"

# Namespace
kubectl create namespace euro-office
```

## Load images into k3d

This avoids pushing to a remote registry during development. Repeat this step every time you rebuild:

```bash
k3d image import ghcr.io/euro-office/cluster-docs:latest -c euro-office
k3d image import ghcr.io/euro-office/cluster-utils:latest -c euro-office
k3d image import ghcr.io/euro-office/cluster-example:latest -c euro-office
```

## Deploy dependencies

```bash
# Database, cache, message broker, shared storage
kubectl apply -f postgres.yaml
kubectl apply -f redis.yaml
kubectl apply -f rabbitmq.yaml
kubectl apply -f storage.yaml

# Wait for the three dependency pods to be Running
kubectl get pods -n euro-office -w
```

Verify connectivity before installing the chart:

```bash
# Postgres
kubectl run pgtest --rm -it --restart=Never -n euro-office \
  --image=postgres:16 -- \
  psql "postgresql://eurooffice:eurooffice@pg-postgresql:5432/eurooffice" -c "SELECT 1;"

# Redis
kubectl run redistest --rm -it --restart=Never -n euro-office \
  --image=redis:7-alpine -- \
  redis-cli -h redis-master -a redis ping
```

Postgres should print `1`, Redis should print `PONG`.

## Install the chart

```bash
helm install docs .. -n euro-office -f my-values.yml --timeout 10m

# Watch the install hooks run, then the deployments come up
kubectl get pods -n euro-office -w
```

Expected sequence:
1. `wopi-keys-gen-*` → Completed
2. `pre-install-*` → Completed
3. `converter-*` → 1/1 Running
4. `docservice-*` → 2/2 Running (docservice + proxy sidecar)
5. `example-0` → 1/1 Running
6. `adminpanel-0` → 1/1 Running

Press Ctrl-C once everything reads Running or Completed.

## Access the demo

```bash
kubectl port-forward -n euro-office svc/documentserver 8082:8888
```

Open http://localhost:8082 in a browser. The welcome page should load. Click into `/example/` to access the demo editor app where you can create and edit documents.

Quick health check:

```bash
curl http://localhost:8082/healthcheck
# returns: true
```

## Updating after a rebuild

When you rebuild any of the images:

```bash
# Reimport the changed image
k3d image import ghcr.io/euro-office/cluster-docs:latest -c euro-office

# Restart the affected workloads to pick up the new image
kubectl rollout restart deployment/docservice deployment/converter -n euro-office
# For the example app (StatefulSet):
kubectl delete pod example-0 -n euro-office
```

## Redis editor-storage examples

The base `my-values.yml` leaves both editor stores in their default `memory`
mode. Apply one of the overlays together with the base values file to select a
Redis topology. Only one Redis topology may be configured at a time.

The standalone overlay works with the `redis.yaml` manifest included here:

```bash
helm install docs .. -n euro-office \
  -f my-values.yml -f my-values.redis-standalone.yml --timeout 10m
```

The data-only overlay demonstrates that the two storage modules can be chosen
independently:

```bash
helm install docs .. -n euro-office \
  -f my-values.yml -f my-values.redis-data-only.yml --timeout 10m
```

The Sentinel and Cluster overlays use the pod names of the test manifests
`redis-sentinel.yaml` and `redis-cluster.yaml`; see the next section. To use your
own Sentinel or Cluster, edit the node names in the overlay.

## Redis Sentinel and Cluster test installs

`redis.yaml` deploys a single Redis, which is enough for the standalone mode.
To test the `sentinel` and `cluster` editor storage modes, including failover, deploy one of these
instead (they are for QA only: no persistence; the images are `valkey/valkey:8.1.12-alpine`,
`redis:7.4-alpine` works as a drop-in replacement). Run **one** topology at a time together with
the dependencies from [Deploy dependencies](#deploy-dependencies), but without `redis.yaml`.
The pods meet the Kubernetes "restricted" Pod Security Standard (non-root uid 999, no privilege escalation, all capabilities dropped, `RuntimeDefault` seccomp).

### Sentinel (2 Redis nodes, 3 Sentinels)

```bash
# Recommended pre-check (validates against the cluster's API and admission, including Pod Security)
kubectl -n euro-office apply --dry-run=server -f redis-sentinel.yaml
kubectl apply -f redis-sentinel.yaml
kubectl rollout status statefulset/redis-node statefulset/redis-sentinel -n euro-office --timeout=3m

helm install docs .. -n euro-office -f my-values.yml -f my-values.redis-sentinel.yml --timeout 10m
```

Checks (the Sentinels run without authentication by default):

```bash
# Every Sentinel must name the same primary
for i in 0 1 2; do
  kubectl exec -n euro-office redis-sentinel-$i -- \
    redis-cli -p 26379 SENTINEL get-master-addr-by-name mymaster
done
kubectl exec -n euro-office redis-sentinel-0 -- redis-cli -p 26379 SENTINEL ckquorum mymaster
```

Failover test:

```bash
# 1. Delete the pod that is the primary (here redis-node-0)
kubectl delete pod redis-node-0 -n euro-office
# 2. Within ~10 s the Sentinels promote the other node: the command below must now print redis-node-1
kubectl exec -n euro-office redis-sentinel-0 -- redis-cli -p 26379 SENTINEL get-master-addr-by-name mymaster
# 3. The deleted pod comes back as a replica of the new primary, never as a second primary
kubectl exec -n euro-office redis-node-0 -- sh -c 'REDISCLI_AUTH=redis redis-cli INFO replication' | grep -E 'role|master_host|master_link_status'
```

Document editing must keep working during and after the failover.

**Sentinel authentication.** Set `sentinel-password` (letters and digits only) in the Secret
`redis-sentinel-auth` and, for a named ACL user, `sentinel-user` as well. The user can only do what the
Sentinel clients of this setup need (authenticate, `PING`, `SENTINEL master|masters|sentinels|replicas`,
`CLIENT SETINFO`, subscribe to the Sentinel event channels), see the comment in `redis-sentinel.yaml`.
It cannot run other commands, for example `SENTINEL get-master-addr-by-name`. Then restart the Sentinels
and the nodes, and upgrade the release with the lines described in the comments of `my-values.redis-sentinel.yml`:

```bash
kubectl edit secret redis-sentinel-auth -n euro-office     # or re-apply the file after editing stringData
kubectl rollout restart statefulset/redis-sentinel statefulset/redis-node -n euro-office
helm upgrade docs .. -n euro-office -f my-values.yml -f my-values.redis-sentinel.yml \
  --set connections.redisSentinelNoPass=false \
  --set connections.redisSentinelUser=<sentinel-user> \
  --set connections.redisSentinelExistingSecret=redis-sentinel-auth \
  --set connections.redisSentinelSecretKeyName=sentinel-password
# password only, without a named user: omit redisSentinelUser
# check with: redis-cli --user <sentinel-user> -a <sentinel-password> -p 26379 SENTINEL master mymaster
```

### Cluster (6 nodes: 3 primaries, 3 replicas)

```bash
# Recommended pre-check (validates against the cluster's API and admission, including Pod Security)
kubectl -n euro-office apply --dry-run=server -f redis-cluster.yaml
kubectl apply -f redis-cluster.yaml
kubectl wait --for=condition=complete job/redis-cluster-init -n euro-office --timeout=3m

helm install docs .. -n euro-office -f my-values.yml -f my-values.redis-cluster.yml --timeout 10m
```

Checks:

```bash
kubectl exec -n euro-office redis-cluster-0 -- sh -c 'REDISCLI_AUTH=redis redis-cli CLUSTER INFO' | grep -E 'cluster_state|cluster_slots_ok|cluster_known_nodes'
kubectl exec -n euro-office redis-cluster-0 -- sh -c 'REDISCLI_AUTH=redis redis-cli CLUSTER NODES'
```

Expected: `cluster_state:ok`, `cluster_slots_ok:16384`, `cluster_known_nodes:6`, three `master` and three `slave` lines.

Failover test (controlled). `CLUSTER FAILOVER` must be sent to a replica; the replica and its primary
coordinate the switch, so no acknowledged write is lost
([CLUSTER FAILOVER](https://valkey.io/commands/cluster-failover/)). The command only schedules the
failover, so an `OK` does not prove that it worked: check the result.

```bash
# 1. Pick a primary and one of its replicas from CLUSTER NODES. The replica line names its primary
#    (the 4th column is the node id of the primary). Here: replica redis-cluster-3.
kubectl exec -n euro-office redis-cluster-0 -- sh -c 'REDISCLI_AUTH=redis redis-cli CLUSTER NODES'
# 2. Run the failover ON THE REPLICA
kubectl exec -n euro-office redis-cluster-3 -- sh -c 'REDISCLI_AUTH=redis redis-cli CLUSTER FAILOVER'
# 3. Verify, after a few seconds: the replica is now a primary and the old primary a replica of it
kubectl exec -n euro-office redis-cluster-3 -- sh -c 'REDISCLI_AUTH=redis redis-cli ROLE | head -n 1'   # master
kubectl exec -n euro-office redis-cluster-0 -- sh -c 'REDISCLI_AUTH=redis redis-cli CLUSTER NODES'
kubectl exec -n euro-office redis-cluster-0 -- sh -c 'REDISCLI_AUTH=redis redis-cli CLUSTER INFO' | grep -E 'cluster_state|cluster_slots_ok'
```

Expected: `redis-cluster-3` shows `master` with the slots of the old primary, the old primary shows `slave`
with `redis-cluster-3` as its primary, `cluster_state:ok`, `cluster_slots_ok:16384`, still three `master`
and three `slave` lines. Run `helm test` afterwards (and, for a stricter check, while a document is open).
With TLS add `--tls --cacert /tls/ca.crt` to `redis-cli`. `CLUSTER FAILOVER FORCE` and `TAKEOVER` skip
parts of the coordination and are for an unreachable primary; they are not needed here.

Node restart test. The data directory is an `emptyDir`, so stop the `redis-server` process (the container
restarts and keeps its node identity) instead of deleting the pod:

```bash
# 1. Pick a primary from CLUSTER NODES (here redis-cluster-1) and stop its server
kubectl exec -n euro-office redis-cluster-1 -- sh -c 'REDISCLI_AUTH=redis redis-cli SHUTDOWN NOSAVE'
# 2. The container restarts; check that the cluster is healthy again
kubectl exec -n euro-office redis-cluster-0 -- sh -c 'REDISCLI_AUTH=redis redis-cli CLUSTER INFO' | grep cluster_state
kubectl exec -n euro-office redis-cluster-0 -- sh -c 'REDISCLI_AUTH=redis redis-cli CLUSTER NODES'
```

This proves that the application survives a node restart and that the node rejoins with its identity. It does
not prove a promotion: the container restarts quickly, which can be before the replicas of the
primary (`--cluster-node-timeout 5000`) have promoted one of them, and then the same node is a primary
again. Use the `CLUSTER FAILOVER` test above to test the promotion.

Deleting a cluster pod (`kubectl delete pod`) also tests the promotion (`cluster_state:ok` again), but the
recreated pod is a new empty node that does not rejoin the cluster by itself: the cluster then lists a
failed `noaddr` node. Recreate the cluster to get back to six nodes (see Clean up).

### TLS (optional)

Both test installs can run with TLS (Redis nodes, Sentinels and the cluster bus) and a private CA. This
tests `connections.redisTLS`, `connections.redisSentinelTLS` and `connections.redisCAExistingSecret`,
including that the Converter starts without the Redis CA. It is off by default; the manifests switch to TLS
when the optional Secret `redis-tls` exists, with the same ports. The certificates are issued by
[cert-manager](https://cert-manager.io/docs/), which must be installed
(`kubectl get crd certificates.cert-manager.io`). `redis-tls-certs.yaml` holds a namespaced self-signed test CA,
a CA Issuer and one server certificate for the three headless Services; the Secret `redis-tls` it produces
holds `tls.crt`, `tls.key` and `ca.crt`.

```bash
# 1. Issue the certificates (recommended pre-check first), then wait until they are Ready
kubectl -n euro-office apply --dry-run=server -f redis-tls-certs.yaml
kubectl apply -f redis-tls-certs.yaml
kubectl -n euro-office wait --for=condition=Ready certificate/redis-test-ca certificate/redis-tls --timeout=120s

# 2. Restart the Redis pods so that they start in TLS mode
kubectl rollout restart statefulset/redis-node statefulset/redis-sentinel -n euro-office      # Sentinel
kubectl rollout restart statefulset/redis-cluster -n euro-office                                # Cluster:
kubectl delete job/redis-cluster-init -n euro-office --ignore-not-found && kubectl apply -f redis-cluster.yaml   # create it again

# 3. Install with the topology overlay and then the TLS overlay
helm install docs .. -n euro-office -f my-values.yml -f my-values.redis-sentinel.yml -f my-values.redis-tls.yml --timeout 10m
# Cluster: -f my-values.redis-cluster.yml instead of the Sentinel overlay
```

The chart takes the CA from the `ca.crt` item of the Secret `redis-tls` only, so the private key in that Secret
is not mounted into DocService, AdminPanel or the test Pods.

Checks:

```bash
# Plain connections are refused, TLS works
kubectl exec -n euro-office redis-sentinel-0 -- redis-cli -p 26379 ping                                  # error / connection reset
kubectl exec -n euro-office redis-sentinel-0 -- redis-cli --tls --cacert /tls/ca.crt -p 26379 SENTINEL get-master-addr-by-name mymaster
kubectl exec -n euro-office redis-cluster-0 -- sh -c 'REDISCLI_AUTH=redis redis-cli --tls --cacert /tls/ca.crt CLUSTER INFO' | grep cluster_state

# The CA reaches the Pods that use Redis, but not the Converter (the test Pod exists only while `helm test` runs)
for w in deploy/docservice statefulset/adminpanel; do
  kubectl exec -n euro-office $w -- printenv NODE_EXTRA_CA_CERTS      # /etc/redis-ca-certificates/ca.crt
done
kubectl get pod/test-ds -n euro-office -o yaml | grep -A1 _CA_CERTS    # the test Pod: REDIS_SERVER_CA_CERTS (and REDIS_SENTINEL_CA_CERTS)
kubectl get deploy/converter -n euro-office -o yaml | grep -c NODE_EXTRA_CA_CERTS     # 0, and the Converter is Ready
helm test docs -n euro-office
```

The failover tests above work the same way with TLS: add `--tls --cacert /tls/ca.crt` to the `redis-cli`
commands (and `redis-cli` needs no extra options for the Sentinel password, which is empty by default).

Negative test: install with the TLS overlay but without the CA, for example with
`--set connections.redisCAExistingSecret=`. The chart renders (the default trust store is used), but the
test CA is unknown: DocService and `helm test` cannot connect to Redis and report
`SELF_SIGNED_CERT_IN_CHAIN`. Install again with `redisCAExistingSecret: redis-tls` to fix it.

Renewal: cert-manager renews the certificates (30 days before they expire) and updates the Secret. The
Valkey 8.1 image used here has no `tls-auto-reload-interval` (the setting is described in the
[Valkey TLS documentation](https://valkey.io/topics/tls/) for newer versions, and `CONFIG GET` returns
nothing on 8.1.12), so restart the Redis pods after a renewal. DocService reads the CA only when its Pod
starts (`NODE_EXTRA_CA_CERTS`), so restart it too (the CA key is kept on renewal, `rotationPolicy: Never`).

To go back to plain Redis: delete the certificates and the Secrets, restart the StatefulSets as above (and
create the cluster again), and install without `my-values.redis-tls.yml`. cert-manager does not delete the
Secrets with the Certificates:

```bash
kubectl delete -f redis-tls-certs.yaml
kubectl delete secret redis-tls redis-test-ca -n euro-office --ignore-not-found
```

### Clean up

```bash
helm uninstall docs -n euro-office
kubectl delete -f redis-sentinel.yaml --ignore-not-found     # or redis-cluster.yaml
```

## Common debugging

```bash
# What's not Ready?
kubectl get pods -n euro-office

# Why is a pod pending/crashing?
kubectl describe pod <pod-name> -n euro-office | tail -20

# Container logs
kubectl logs <pod-name> -n euro-office -c <container-name> --tail=100
kubectl logs -n euro-office -l app=docservice --tail=100   # by label

# Render chart manifests without installing (sanity-check overrides)
helm template docs .. -n euro-office -f my-values.yml | less

# Reset everything quickly during dev (much faster than helm uninstall)
k3d cluster delete euro-office
```

## Tear down

Clean uninstall of just the application (keeps DBs, NFS, etc.):

```bash
helm uninstall docs -n euro-office --no-hooks
kubectl delete jobs -n euro-office --all
```

Full teardown (recommended for dev — leaves a clean slate):

```bash
k3d cluster delete euro-office
sudo rm -rf /tmp/euro-office-shared
```

## Files in this directory

- `postgres.yaml` — Postgres deployment, service, and secret
- `redis.yaml` — Redis deployment, service, and secret
- `redis-sentinel.yaml` — test Redis Sentinel: 2 Redis nodes, 3 Sentinels, secret (QA only)
- `redis-cluster.yaml` — test Redis Cluster: 6 nodes, init Job, secret (QA only)
- `rabbitmq.yaml` — RabbitMQ deployment, service, and secret
- `storage.yaml` — `nfs` StorageClass and static PVs backed by the host-mounted shared directory
- `my-values.yml` — Helm chart overrides (image references, connection URLs, JWT secret, env vars)
- `my-values.redis-standalone.yml` — enables Redis for both editor data and statistics using standalone Redis
- `my-values.redis-data-only.yml` — enables Redis for editor data while keeping statistics in memory
- `my-values.redis-sentinel.yml` — Sentinel configuration overlay for the test Sentinel in `redis-sentinel.yaml` (or any reachable Redis Sentinel deployment)
- `redis-tls-certs.yaml` — cert-manager Issuers and Certificates (test CA and the Secret `redis-tls`) for the optional TLS mode of the Sentinel and Cluster test installs
- `my-values.redis-tls.yml` — TLS overlay (after a Sentinel or Cluster overlay)
- `my-values.redis-cluster.yml` — Cluster configuration overlay for the test cluster in `redis-cluster.yaml` (or any reachable Redis Cluster deployment)

## Limitations

This setup is for local testing only:

- All passwords (`eurooffice`, `redis`, `rabbit`, JWT secret) are dev placeholders — never use them in production
- The shared host volume at `/tmp/euro-office-shared` only works because k3d nodes run on a single host; in a real cluster, replace `storage.yaml` with proper NFS or another RWX provisioner
- Single-replica databases with no backups
- No TLS, no ingress, port-forwarding only
