cluster_name := "quickwit-poc"
ns := "observability"

default:
    @just --list

# Create the kind cluster and deploy everything.
up: cluster minio quickwit vector generator grafana
    @echo "Ready. Run 'just dashboard' for the Quickwit UI, or 'just dashboard-grafana' for Grafana."

# Create the kind cluster.
cluster:
    kind create cluster --name {{cluster_name}} --config kind-config.yaml
    kubectl apply -f manifests/namespace.yaml

# Deploy MinIO (S3-compatible storage for Quickwit) and wait for it.
minio:
    kubectl apply -f manifests/minio.yaml
    kubectl -n {{ns}} rollout status deployment/minio --timeout=120s
    kubectl -n {{ns}} wait --for=condition=complete job/minio-create-bucket --timeout=120s

# Install Quickwit via Helm and create the k8s-logs index.
quickwit:
    helm repo add quickwit https://helm.quickwit.io
    helm repo update quickwit
    helm upgrade --install quickwit quickwit/quickwit -n {{ns}} -f helm/quickwit-values.yaml
    kubectl -n {{ns}} rollout status deployment/quickwit-metastore --timeout=180s
    kubectl -n {{ns}} rollout status statefulset/quickwit-indexer --timeout=180s
    just create-index

# Create the k8s-logs index via the Quickwit REST API.
create-index:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl -n {{ns}} port-forward svc/quickwit-metastore 7280:7280 >/tmp/qw-pf.log 2>&1 &
    pf_pid=$!
    trap "kill $pf_pid" EXIT
    for i in $(seq 1 20); do
      curl -sf http://localhost:7280/health/livez >/dev/null && break
      sleep 2
    done
    curl -s -o /dev/null -w "%{http_code}\n" -XPOST http://localhost:7280/api/v1/indexes \
      -H 'content-type: application/yaml' \
      --data-binary @quickwit/k8s-logs-index.yaml

# Install Vector via Helm to ship container logs to Quickwit.
vector:
    helm repo add vector https://helm.vector.dev
    helm repo update vector
    helm upgrade --install vector vector/vector -n {{ns}} -f helm/vector-values.yaml
    kubectl -n {{ns}} rollout status daemonset/vector --timeout=120s

# Deploy the synthetic log generator.
generator:
    kubectl apply -f manifests/log-generator.yaml
    kubectl -n {{ns}} rollout status deployment/log-generator --timeout=60s

# Install Grafana via Helm with the Quickwit datasource plugin and a prebuilt dashboard.
grafana:
    helm repo add grafana https://grafana.github.io/helm-charts
    helm repo update grafana
    helm upgrade --install grafana grafana/grafana -n {{ns}} -f helm/grafana-values.yaml
    kubectl -n {{ns}} rollout status deployment/grafana --timeout=180s

# Port-forward the Quickwit UI at http://localhost:7280.
dashboard:
    @echo "Quickwit UI: http://localhost:7280 (no login required)"
    kubectl -n {{ns}} port-forward svc/quickwit-searcher 7280:7280

# Port-forward Grafana at http://localhost:3000.
dashboard-grafana:
    @echo "Grafana: http://localhost:3000 (no login required, anonymous access enabled)"
    kubectl -n {{ns}} port-forward svc/grafana 3000:80

# Run a search query against the k8s-logs index (requires 'just dashboard' running elsewhere, or use search-once).
search query="*":
    curl -s "http://localhost:7280/api/v1/k8s-logs/search?query={{query}}" | jq .

# Port-forward on demand and run a single search query.
search-once query="*":
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl -n {{ns}} port-forward svc/quickwit-searcher 7280:7280 >/tmp/qw-pf.log 2>&1 &
    pf_pid=$!
    trap "kill $pf_pid" EXIT
    sleep 3
    curl -s "http://localhost:7280/api/v1/k8s-logs/search?query={{query}}" | jq .

# Show pod status for the observability namespace.
status:
    kubectl -n {{ns}} get pods

# Tail Vector's own logs.
logs-vector:
    kubectl -n {{ns}} logs -l app.kubernetes.io/instance=vector -f --tail=100

# Tail the synthetic log generator's logs.
logs-generator:
    kubectl -n {{ns}} logs -l app=log-generator -f --tail=100

# Tear down the entire kind cluster.
down:
    kind delete cluster --name {{cluster_name}}
