import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from common import ENGINES, format_bytes

QUERY_EXPLANATIONS = {
    "match_all": "baseline: return any results, no filtering",
    "term_filter": 'level=ERROR, an indexed/labeled field on all three engines',
    "text_search": 'substring search for "failed" in the log body',
    "point_lookup": "exact match on request_id, a high-cardinality field NOT indexed as a Loki "
                     "label -- should hurt Loki most, and is the point of the comparison",
    "time_window": "same as match_all, scoped to the last 5 minutes",
}
QUERY_ORDER = ["match_all", "term_filter", "text_search", "point_lookup", "time_window"]


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def print_ingest(data):
    print("=== Ingest ===\n")
    present = [e for e in ENGINES if e in data]
    if not present:
        print("(no ingest results yet -- run `just bench-ingest`)\n")
        return

    header = f"{'engine':<14}{'docs':>12}{'size':>10}{'time':>8}{'docs/s':>10}{'MB/s':>8}{'failed':>8}"
    print(header)
    print("-" * len(header))
    best_engine, best_rate = None, -1
    for e in present:
        r = data[e]
        print(f"{e:<14}{r['docs']:>12,}{format_bytes(r['bytes']):>10}{r['elapsed_s']:>7.1f}s"
              f"{r['docs_per_s']:>10,.0f}{r['bytes_per_s'] / (1 << 20):>8.1f}{r['failed_batches']:>8}")
        if r["docs_per_s"] > best_rate:
            best_engine, best_rate = e, r["docs_per_s"]
    if len(present) > 1:
        print(f"\nfastest ingest: {best_engine} ({best_rate:,.0f} docs/s)")
    print()


def print_query(data):
    print("=== Query latency (p50 / p95 ms) ===\n")
    present = [e for e in ENGINES if e in data]
    if not present:
        print("(no query results yet -- run `just bench-query`)\n")
        return

    col_w = 18
    header = f"{'query':<14}" + "".join(f"{e:<{col_w}}" for e in present)
    print(header)
    print("-" * len(header))
    for q in QUERY_ORDER:
        row = f"{q:<14}"
        for e in present:
            r = data.get(e, {}).get(q)
            cell = f"{r['p50_ms']:.1f} / {r['p95_ms']:.1f}" if r else "-"
            if r and r["status"] != 200:
                cell += f" (HTTP {r['status']})"
            row += f"{cell:<{col_w}}"
        print(row)
    print()

    print("what each query means:")
    for q in QUERY_ORDER:
        print(f"  {q:<13}- {QUERY_EXPLANATIONS[q]}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest-results", default="bench/out/ingest_results.json")
    ap.add_argument("--query-results", default="bench/out/query_results.json")
    args = ap.parse_args()

    print_ingest(load(args.ingest_results))
    print_query(load(args.query_results))


if __name__ == "__main__":
    main()
