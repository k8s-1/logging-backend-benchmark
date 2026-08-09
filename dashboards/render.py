#!/usr/bin/env python3
"""Renders the k8s-logs Grafana dashboard for each log engine from one panel layout.

Panel structure (titles, gridPos, panel types, templating vars) is identical across
engines. Only the datasource reference and each target's query schema differ, since
Quickwit/VictoriaLogs and Loki have fundamentally different query languages and
index models (see README's cardinality note).
"""
import json
import os

OUT_DIR = os.path.join(os.path.dirname(__file__), "out")


def quickwit_targets():
    datasource = {"type": "quickwit-quickwit-datasource", "uid": "quickwit-k8s-logs"}
    volume = {
        "refId": "A",
        "datasource": datasource,
        "query": "${search:raw}",
        "metrics": [{"id": "1", "type": "count"}],
        "bucketAggs": [
            {
                "id": "2",
                "type": "date_histogram",
                "field": "timestamp",
                "settings": {"interval": "auto"},
            }
        ],
    }
    logs = {
        "refId": "A",
        "datasource": datasource,
        "query": "${search:raw}",
        "metrics": [{"id": "1", "type": "logs", "settings": {"limit": "100"}}],
        "bucketAggs": [],
    }
    return datasource, volume, logs


def loki_targets():
    datasource = {"type": "loki", "uid": "loki"}
    # Only level/module are indexed labels (see README); everything else is line
    # content, so the search box is a LogQL line filter, not a label selector.
    volume = {
        "refId": "A",
        "datasource": datasource,
        "queryType": "range",
        "expr": 'sum(count_over_time({level=~".+"} |~ `${search:raw}` [$__interval]))',
    }
    logs = {
        "refId": "A",
        "datasource": datasource,
        "queryType": "range",
        "expr": '{level=~".+"} |~ `${search:raw}`',
        "maxLines": 100,
    }
    return datasource, volume, logs


def victorialogs_targets():
    datasource = {"type": "victoriametrics-logs-datasource", "uid": "victorialogs"}
    volume = {
        "refId": "A",
        "datasource": datasource,
        "queryType": "hits",
        "expr": "${search:raw}",
    }
    logs = {
        "refId": "A",
        "datasource": datasource,
        "queryType": "instant",
        "expr": "${search:raw}",
        "maxLines": 100,
    }
    return datasource, volume, logs


# name -> (dashboard title suffix, search box default, target builder)
ENGINES = {
    "quickwit": ("Quickwit", "*", quickwit_targets),
    "loki": ("Loki", ".*", loki_targets),
    "victorialogs": ("VictoriaLogs", "*", victorialogs_targets),
}


def build_dashboard(engine, title_suffix, search_default, targets_fn):
    datasource, volume_target, logs_target = targets_fn()
    return {
        "title": f"Kubernetes Logs ({title_suffix})",
        "uid": f"k8s-logs-{engine}",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "refresh": "10s",
        "time": {"from": "now-15m", "to": "now"},
        "templating": {
            "list": [
                {
                    "name": "search",
                    "type": "textbox",
                    "label": "Search",
                    "query": search_default,
                    "current": {"text": search_default, "value": search_default},
                },
                {
                    "name": "Filters",
                    "type": "adhoc",
                    "label": "Filters",
                    "datasource": datasource,
                    "filters": [],
                },
            ]
        },
        "panels": [
            {
                "id": 1,
                "title": "Log volume",
                "type": "timeseries",
                "gridPos": {"h": 8, "w": 24, "x": 0, "y": 0},
                "datasource": datasource,
                "targets": [volume_target],
            },
            {
                "id": 4,
                "title": "Matching logs",
                "type": "logs",
                "gridPos": {"h": 12, "w": 24, "x": 0, "y": 8},
                "datasource": datasource,
                "options": {
                    "wrapLogMessage": True,
                    "prettifyLogMessage": True,
                    "enableLogDetails": True,
                },
                "targets": [logs_target],
            },
        ],
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for engine, (title_suffix, search_default, targets_fn) in ENGINES.items():
        dashboard = build_dashboard(engine, title_suffix, search_default, targets_fn)
        out_path = os.path.join(OUT_DIR, f"{engine}.json")
        with open(out_path, "w") as f:
            json.dump(dashboard, f, indent=2)
            f.write("\n")
        print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
