cluster_name := "quickwit-poc"
ns := "observability"

default:
    @just --list

# Create the kind cluster and deploy everything.
up: cluster metrics-server minio quickwit loki victorialogs vector generator grafana
    @echo "Ready. Run 'just dashboard' for the Quickwit UI, or 'just dashboard-grafana' for Grafana."

# Create the kind cluster.
cluster:
    kind create cluster --name {{cluster_name}} --config kind-config.yaml
    kubectl apply -f manifests/namespace.yaml

# Install metrics-server (patched for kind's self-signed kubelet certs + our control-plane taint) so `kubectl top` works.
metrics-server:
    kubectl apply -f manifests/metrics-server.yaml
    kubectl -n kube-system rollout status deployment/metrics-server --timeout=90s

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

# Install Loki via Helm (single-binary mode, MinIO-backed storage).
loki:
    helm repo add grafana https://grafana.github.io/helm-charts
    helm repo update grafana
    helm upgrade --install loki grafana/loki -n {{ns}} -f helm/loki-values.yaml
    kubectl -n {{ns}} rollout status statefulset/loki --timeout=180s

# Install VictoriaLogs via Helm (single-node, local disk storage).
victorialogs:
    helm repo add vm https://victoriametrics.github.io/helm-charts/
    helm repo update vm
    helm upgrade --install victorialogs vm/victoria-logs-single -n {{ns}} -f helm/victorialogs-values.yaml
    kubectl -n {{ns}} rollout status statefulset/victorialogs --timeout=120s

# Install Vector via Helm to ship container logs to Quickwit, Loki, and VictoriaLogs.
vector:
    helm repo add vector https://helm.vector.dev
    helm repo update vector
    helm upgrade --install vector vector/vector -n {{ns}} -f helm/vector-values.yaml
    kubectl -n {{ns}} rollout status daemonset/vector --timeout=120s

# Deploy the synthetic log generator.
generator:
    kubectl apply -f manifests/log-generator.yaml
    kubectl -n {{ns}} rollout status deployment/log-generator --timeout=60s

# Render the per-engine dashboards and load them into a ConfigMap Grafana provisions from.
dashboards:
    python3 dashboards/render.py
    kubectl -n {{ns}} create configmap grafana-dashboards --from-file=dashboards/out -o yaml --dry-run=client | kubectl apply -f -

# Install Grafana via Helm with the Quickwit/Loki/VictoriaLogs datasources and the rendered dashboards.
grafana: dashboards
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

# Generate a synthetic high-cardinality log corpus for benchmarking (scale -size up to 50GB once the pipeline's verified).
bench-generate size="1GB" out="logsample/data":
    cd logsample && go run generate.go -out ../{{out}} -size {{size}}

# Remove the generated log corpus. Safe any time after ingestion succeeds -- each engine holds its own copy.
bench-clean:
    rm -rf logsample/data

# Port-forward quickwit-indexer:7280, loki:3100, and victorialogs:9428 together. Ctrl-C stops all three.
bench-forward:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl -n {{ns}} port-forward svc/quickwit-indexer 7280:7280 &
    pid1=$!
    kubectl -n {{ns}} port-forward svc/loki 3100:3100 &
    pid2=$!
    kubectl -n {{ns}} port-forward svc/victorialogs 9428:9428 &
    pid3=$!
    trap "kill $pid1 $pid2 $pid3 2>/dev/null || true" EXIT
    wait

# Ingest the generated corpus into one engine ("quickwit"/"loki"/"victorialogs") or "all", bypassing Vector/k8s.
# Requires `just bench-forward` running in another terminal.
bench-ingest engine="all":
    python3 bench/ingest.py --engine {{engine}}

# Run a fixed 5-query set against one engine or "all" and report p50/p95/max latency per query.
bench-query engine="all":
    python3 bench/query.py --engine {{engine}}

# Full flow: generate (skipped if logsample/data already has a corpus) -> port-forward -> ingest -> query -> cleanup.
bench size="200MB" engine="all":
    #!/usr/bin/env bash
    set -euo pipefail
    if ls logsample/data/*.ndjson >/dev/null 2>&1; then
        echo "logsample/data already has a corpus, reusing it (run 'just bench-generate {{size}}' first to regenerate)"
    else
        just bench-generate {{size}}
    fi
    kubectl -n {{ns}} port-forward svc/quickwit-indexer 7280:7280 >/tmp/bench-qw-pf.log 2>&1 &
    pid1=$!
    kubectl -n {{ns}} port-forward svc/loki 3100:3100 >/tmp/bench-loki-pf.log 2>&1 &
    pid2=$!
    kubectl -n {{ns}} port-forward svc/victorialogs 9428:9428 >/tmp/bench-vl-pf.log 2>&1 &
    pid3=$!
    trap "kill $pid1 $pid2 $pid3 2>/dev/null || true" EXIT
    sleep 3
    just bench-ingest {{engine}}
    just bench-query {{engine}}
    just bench-report

# Print a comparison report from the last bench-ingest/bench-query results.
bench-report:
    python3 bench/report.py

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
