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

# Wait for all four to be Running
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
5. `adminpanel-0` → 1/1 Running

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
- `rabbitmq.yaml` — RabbitMQ deployment, service, and secret
- `storage.yaml` — `nfs` StorageClass and static PVs backed by the host-mounted shared directory
- `my-values.yml` — Helm chart overrides (image references, connection URLs, JWT secret, env vars)

## Limitations

This setup is for local testing only:

- All passwords (`onlyoffice`, `redis`, `rabbit`, JWT secret) are dev placeholders — never use them in production
- The shared host volume at `/tmp/euro-office-shared` only works because k3d nodes run on a single host; in a real cluster, replace `storage.yaml` with proper NFS or another RWX provisioner
- Single-replica databases with no backups
- No TLS, no ingress, port-forwarding only
