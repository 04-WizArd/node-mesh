#!/usr/bin/env python3
"""Rough throughput/latency benchmark through the public API.

Measures end-to-end upload (client -> coordinator -> RF nodes, quorum) and
verified download (node -> coordinator spool -> client) for a few sizes and
concurrency levels. Results depend heavily on disks, network and the
coordinator's worker count; treat them as indicative, not as capacity claims.

    python3 scripts/benchmark.py --url http://127.0.0.1:8080 --username demo --password ...
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cluster_demo import Api

MIB = 1024 * 1024


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


def run_case(api: Api, size: int, concurrency: int, count: int) -> dict:
    payloads = [os.urandom(size) for _ in range(count)]

    def up(data: bytes) -> tuple[float, str]:
        start = time.perf_counter()
        status, body = api.upload("bench.bin", data)
        if status != 201:
            raise RuntimeError(f"upload failed: HTTP {status} {body}")
        return time.perf_counter() - start, body["file"]["id"]

    def down(file_id: str) -> float:
        start = time.perf_counter()
        status, _ = api.download(file_id)
        if status != 200:
            raise RuntimeError(f"download failed: HTTP {status}")
        return time.perf_counter() - start

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        t0 = time.perf_counter()
        uploads = list(pool.map(up, payloads))
        up_wall = time.perf_counter() - t0
        t0 = time.perf_counter()
        downloads = list(pool.map(down, [fid for _, fid in uploads]))
        down_wall = time.perf_counter() - t0

    up_lat = [lat for lat, _ in uploads]
    total_mib = size * count / MIB
    for _, fid in uploads:
        api.request("DELETE", f"/api/v1/files/{fid}/", headers={"X-CSRFToken": api._csrf()})
    return {
        "size_mib": size / MIB,
        "concurrency": concurrency,
        "count": count,
        "upload_mib_s": total_mib / up_wall,
        "upload_p50_s": statistics.median(up_lat),
        "upload_p95_s": percentile(up_lat, 95),
        "download_mib_s": total_mib / down_wall,
        "download_p50_s": statistics.median(downloads),
        "download_p95_s": percentile(downloads, 95),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--username", default="demo")
    parser.add_argument("--password", default=os.environ.get("DEMO_PASSWORD"), required=False)
    parser.add_argument("--sizes", default="0.1,1,10,50", help="MiB, comma separated")
    parser.add_argument("--concurrency", default="1,4")
    parser.add_argument("--count", type=int, default=8)
    args = parser.parse_args()
    if not args.password:
        parser.error("--password (or DEMO_PASSWORD) is required")

    api = Api(args.url)
    api.login(args.username, args.password)
    header = (
        f"{'size MiB':>8} {'conc':>4} {'n':>3} | {'up MiB/s':>8} {'p50 s':>6} {'p95 s':>6} |"
        f" {'down MiB/s':>10} {'p50 s':>6} {'p95 s':>6}"
    )
    print(header)
    print("-" * len(header))
    for size in (float(s) for s in args.sizes.split(",")):
        for conc in (int(c) for c in args.concurrency.split(",")):
            r = run_case(api, int(size * MIB), conc, args.count)
            print(
                f"{r['size_mib']:>8.1f} {conc:>4} {args.count:>3} | {r['upload_mib_s']:>8.1f} "
                f"{r['upload_p50_s']:>6.2f} {r['upload_p95_s']:>6.2f} | {r['download_mib_s']:>10.1f} "
                f"{r['download_p50_s']:>6.2f} {r['download_p95_s']:>6.2f}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
