# quickwit-ingestion

A disposable, local Kubernetes POC for a log observability pipeline: [Vector](https://vector.dev)
ships container logs into [Quickwit](https://quickwit.io) for storage/search, with a
[Grafana](https://grafana.com) dashboard on top. Everything runs in a [kind](https://kind.sigs.k8s.io)
cluster and is driven by a `justfile`, so the whole stack comes up and tears down with one command
each.

This is a POC, not a production reference: storage is ephemeral (`emptyDir`), credentials are
dev-only placeholders, and Grafana has anonymous access enabled. Don't run it outside a local
sandbox as-is.

## Architecture

```
log-generator (synthetic JSON logs, stdout)
      │
      ▼
containerd / kubelet log files
      │
      ▼
Vector (DaemonSet, kubernetes_logs source + VRL remap)
      │  Elasticsearch-bulk API
      ▼
Quickwit (indexer / searcher / metastore / control-plane / janitor)
      │  S3 API
      ▼
MinIO (object storage backing Quickwit)

Grafana ──(quickwit-quickwit-datasource plugin)──▶ Quickwit searcher
```

## Prerequisites

- [kind](https://kind.sigs.k8s.io)
- [helm](https://helm.sh)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [just](https://github.com/casey/just)
- Docker (or another kind-compatible container runtime)

## Quickstart

```sh
just up
```

Brings up a kind cluster and installs, in order: MinIO, Quickwit (+ the `k8s-logs` index), Vector,
a synthetic log generator, and Grafana.

```sh
just dashboard-grafana   # http://localhost:3000 — Grafana dashboard, no login required
just dashboard           # http://localhost:7280 — raw Quickwit search UI
```

Grafana's **Kubernetes Logs (Quickwit)** dashboard has a log-volume timeline on top (drag to zoom
the time range) and matching log lines below, with a free-text **Search** box and a dynamic
**+ Filters** button (field/value pairs discovered live from the index — try `module` or `level`).

Tear it all down:

```sh
just down
```

Run `just` with no arguments to list every available recipe (rebuilding just the index, tailing
Vector/generator logs, running one-off searches, etc).

## Layout

| Path | Purpose |
|---|---|
| `kind-config.yaml` | Single-node kind cluster definition |
| `manifests/namespace.yaml` | `observability` namespace |
| `manifests/minio.yaml` | MinIO (S3-compatible storage for Quickwit) + bucket-creation job |
| `manifests/log-generator.yaml` | Synthetic log producer (fake Java-style app logs, JSON on stdout) |
| `helm/quickwit-values.yaml` | Quickwit Helm values (MinIO-backed storage) |
| `helm/vector-values.yaml` | Vector Helm values (DaemonSet log collection + parsing + Quickwit sink) |
| `helm/grafana-values.yaml` | Grafana Helm values (Quickwit datasource plugin, dashboard, anonymous auth) |
| `quickwit/k8s-logs-index.yaml` | Quickwit index definition for `k8s-logs` |
| `justfile` | All operational commands |

## Notes on non-obvious decisions

- **MinIO instead of local disk for Quickwit storage**: Quickwit runs as several separate pods
  (indexer, searcher, metastore, ...) that all need to see the same index data. A shared
  `emptyDir`/`hostPath` doesn't work across pods reliably in kind, so MinIO gives them a common
  S3-compatible backend instead — same pattern Quickwit's own docs use for non-local deployments.
- **`level` and `module` are explicit fast fields** in the index mapping, not left to dynamic
  inference. Quickwit's Terms aggregation (used for Grafana's ad hoc filter value picker and any
  breakdown-by-field panel) only works on fields marked `fast: true`.
- **Grafana panel queries use `${var:raw}`**, not plain `$var`. The Quickwit datasource plugin
  Lucene-escapes template variable values by default (so a variable value of `*` becomes the
  literal `\*`, and `level:ERROR` becomes `level\:ERROR`) — `:raw` skips that escaping.
- **Grafana ad hoc filters, not a hardcoded dropdown**, drive field-based filtering. The plugin
  implements `getTagKeys`/`getTagValues`, so the field and value lists are discovered live from
  whatever's actually in the index rather than hardcoded per app/schema.
- **Grafana has anonymous (Admin-role) access enabled** and no persistent storage. Every
  `helm upgrade` recreates the pod with a fresh in-memory session store, so requiring login would
  mean re-authenticating after every dashboard change — fine for a POC, not something to carry
  into a real deployment.

## Why S3 (not local disk)

Object storage isn't incidental here — it's how Quickwit is designed to run. Its indexer,
searcher, metastore, and control-plane are separate, stateless pods that all need to see the same
index data; object storage (S3 or an S3-compatible store) is what lets them share it without a
shared filesystem. Local disk only really works for a single-node setup, which is why this POC
runs MinIO instead of using `emptyDir`/`hostPath` for Quickwit's data.

Moving from this POC to real AWS S3 is mostly a config swap, not an architecture change: point
`helm/quickwit-values.yaml`'s `config.storage.s3` block (and `default_index_root_uri`) at a real
bucket and credentials instead of MinIO, and drop the `flavor: minio` line. Everything else —
the index mapping, Vector pipeline, Grafana dashboard — stays the same.
