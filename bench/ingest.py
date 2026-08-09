import argparse
import glob
import json
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from common import DEFAULT_URLS, ENGINES, http_post, format_bytes, save_result

BATCH_SIZE = 2000
SAMPLE_EVERY = 5000


def parse_record(raw_line):
    try:
        rec = json.loads(raw_line)
        if isinstance(rec, dict):
            return rec
    except json.JSONDecodeError:
        pass
    return {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "message": raw_line}


def to_ns(timestamp_str):
    try:
        dt = datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return str(int(dt.timestamp() * 1e9))
    except (ValueError, TypeError):
        return str(int(time.time() * 1e9))


def send_quickwit(base_url, docs):
    lines = []
    for doc in docs:
        lines.append('{"create":{"_index":"k8s-logs"}}')
        lines.append(json.dumps(doc))
    body = ("\n".join(lines) + "\n").encode()
    status, _ = http_post(f"{base_url}/api/v1/_elastic/_bulk", body)
    return status, len(body)


def send_victorialogs(base_url, docs):
    lines = []
    for doc in docs:
        lines.append('{"create":{"_index":"k8s-logs"}}')
        lines.append(json.dumps(doc))
    body = ("\n".join(lines) + "\n").encode()
    url = f"{base_url}/insert/elasticsearch/_bulk?_msg_field=message&_time_field=timestamp&_stream_fields=level,module"
    status, _ = http_post(url, body)
    return status, len(body)


def send_loki(base_url, docs):
    streams = {}
    for doc in docs:
        level = doc.get("level", "unknown")
        module = doc.get("module", "unknown")
        key = (level, module)
        entry = [to_ns(doc.get("timestamp")), json.dumps(doc)]
        streams.setdefault(key, []).append(entry)
    payload = {
        "streams": [
            {"stream": {"level": lvl, "module": mod}, "values": vals}
            for (lvl, mod), vals in streams.items()
        ]
    }
    body = json.dumps(payload).encode()
    status, _ = http_post(f"{base_url}/loki/api/v1/push", body)
    return status, len(body)


SENDERS = {"quickwit": send_quickwit, "loki": send_loki, "victorialogs": send_victorialogs}


def iter_batches(paths, batch_size):
    batch = []
    for path in paths:
        with open(path, "r") as f:
            for line in f:
                line = line.rstrip("\n")
                if line:
                    batch.append(line)
                if len(batch) >= batch_size:
                    yield batch
                    batch = []
    if batch:
        yield batch


def ingest_engine(engine, base_url, paths, batch_size, workers, samples_out, results_out):
    sender = SENDERS[engine]
    total_docs = 0
    total_bytes = 0
    errors = 0
    samples = []
    counter = 0
    max_inflight = workers * 4

    def process_batch(raw_lines):
        docs = [parse_record(raw) for raw in raw_lines]
        status, nbytes = sender(base_url, docs)
        return status, nbytes, docs

    def collect(fut):
        nonlocal total_docs, total_bytes, errors, counter
        status, nbytes, docs = fut.result()
        if status == 0 or status >= 300:
            errors += 1
            return
        total_docs += len(docs)
        total_bytes += nbytes
        for doc in docs:
            counter += 1
            if counter % SAMPLE_EVERY == 0 and "request_id" in doc:
                samples.append(doc["request_id"])

    start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        inflight = set()
        for batch in iter_batches(paths, batch_size):
            inflight.add(pool.submit(process_batch, batch))
            if len(inflight) >= max_inflight:
                done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done:
                    collect(fut)
        for fut in as_completed(inflight):
            collect(fut)

    elapsed = time.time() - start
    print(f"[{engine}] {total_docs} docs, {format_bytes(total_bytes)}, {elapsed:.1f}s, "
          f"{total_docs / elapsed:.0f} docs/s, {format_bytes(total_bytes / elapsed)}/s, {errors} failed batches")

    if samples_out and samples:
        os.makedirs(os.path.dirname(samples_out), exist_ok=True)
        with open(samples_out, "w") as f:
            json.dump(samples, f)

    if results_out:
        save_result(results_out, engine, {
            "docs": total_docs,
            "bytes": total_bytes,
            "elapsed_s": round(elapsed, 2),
            "docs_per_s": round(total_docs / elapsed, 1) if elapsed else 0,
            "bytes_per_s": round(total_bytes / elapsed, 1) if elapsed else 0,
            "failed_batches": errors,
        })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=ENGINES + ["all"], default="all")
    ap.add_argument("--data", default="logsample/data")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--samples-out", default="bench/out/sample_ids.json")
    ap.add_argument("--results-out", default="bench/out/ingest_results.json")
    for e in ENGINES:
        ap.add_argument(f"--{e}-url", default=DEFAULT_URLS[e])
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(args.data, "*.ndjson")))
    if not paths:
        print(f"no shard files found in {args.data}", file=sys.stderr)
        sys.exit(1)

    targets = ENGINES if args.engine == "all" else [args.engine]
    for engine in targets:
        base_url = getattr(args, f"{engine}_url")
        ingest_engine(engine, base_url, paths, args.batch_size, args.workers, args.samples_out, args.results_out)


if __name__ == "__main__":
    main()
