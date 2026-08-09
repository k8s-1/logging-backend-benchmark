import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from common import DEFAULT_URLS, ENGINES, http_get, percentile, save_result

REPS = 20
WARMUP = 3
QUERIES = ["match_all", "term_filter", "text_search", "point_lookup", "time_window"]


def load_sample_id(path):
    try:
        with open(path) as f:
            samples = json.load(f)
            return samples[0] if samples else "00000-00000"
    except (FileNotFoundError, json.JSONDecodeError):
        return "00000-00000"


def epoch(seconds_ago=0):
    return int(time.time()) - seconds_ago


def quickwit_request(base, name, sample_id):
    url = f"{base}/api/v1/k8s-logs/search"
    params = {
        "match_all": {"query": "*", "max_hits": 20},
        "term_filter": {"query": "level:ERROR", "max_hits": 20},
        "text_search": {"query": "failed", "max_hits": 20},
        "point_lookup": {"query": f'request_id:"{sample_id}"', "max_hits": 5},
        "time_window": {"query": "*", "max_hits": 20, "start_timestamp": epoch(300), "end_timestamp": epoch(0)},
    }[name]
    return url, params


def loki_request(base, name, sample_id):
    exprs = {
        "match_all": '{level=~".+"}',
        "term_filter": '{level="ERROR"}',
        "text_search": '{level=~".+"} |= "failed"',
        "point_lookup": f'{{level=~".+"}} |= "{sample_id}"',
        "time_window": '{level=~".+"}',
    }
    start = epoch(300 if name == "time_window" else 3600)
    params = {"query": exprs[name], "limit": 20, "start": f"{start}000000000", "end": f"{epoch(0)}000000000"}
    return f"{base}/loki/api/v1/query_range", params


def victorialogs_request(base, name, sample_id):
    exprs = {
        "match_all": "*",
        "term_filter": "level:ERROR",
        "text_search": "failed",
        "point_lookup": f'request_id:"{sample_id}"',
        "time_window": "*",
    }
    start = epoch(300 if name == "time_window" else 3600)
    params = {"query": exprs[name], "limit": 20, "start": start, "end": epoch(0)}
    return f"{base}/select/logsql/query", params


REQUEST_BUILDERS = {"quickwit": quickwit_request, "loki": loki_request, "victorialogs": victorialogs_request}


def run_query(engine, base_url, name, sample_id, reps):
    url, params = REQUEST_BUILDERS[engine](base_url, name, sample_id)
    latencies = []
    status = None
    for i in range(WARMUP + reps):
        t0 = time.time()
        status, _ = http_get(url, params)
        dt_ms = (time.time() - t0) * 1000
        if i >= WARMUP:
            latencies.append(dt_ms)
    latencies.sort()
    return status, latencies


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=ENGINES + ["all"], default="all")
    ap.add_argument("--reps", type=int, default=REPS)
    ap.add_argument("--samples", default="bench/out/sample_ids.json")
    ap.add_argument("--results-out", default="bench/out/query_results.json")
    for e in ENGINES:
        ap.add_argument(f"--{e}-url", default=DEFAULT_URLS[e])
    args = ap.parse_args()

    sample_id = load_sample_id(args.samples)
    targets = ENGINES if args.engine == "all" else [args.engine]

    print(f"{'engine':<14}{'query':<14}{'status':<8}{'p50(ms)':<10}{'p95(ms)':<10}{'max(ms)':<10}")
    for engine in targets:
        base_url = getattr(args, f"{engine}_url")
        engine_results = {}
        for name in QUERIES:
            status, latencies = run_query(engine, base_url, name, sample_id, args.reps)
            p50 = percentile(latencies, 0.5)
            p95 = percentile(latencies, 0.95)
            mx = max(latencies) if latencies else 0.0
            print(f"{engine:<14}{name:<14}{str(status):<8}{p50:<10.1f}{p95:<10.1f}{mx:<10.1f}")
            engine_results[name] = {"status": status, "p50_ms": round(p50, 2), "p95_ms": round(p95, 2), "max_ms": round(mx, 2)}
        if args.results_out:
            save_result(args.results_out, engine, engine_results)


if __name__ == "__main__":
    main()
