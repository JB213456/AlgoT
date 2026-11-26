"""Benchmark concurrent ingestion throughput.

Usage: python benchmarks/bench_ingest.py [--rows-per-batch N] [--batches N] [--workers N]
"""
from __future__ import annotations

import argparse
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_data_db import DBConfig, MarketDataDB
from market_data_db.schema import Dataset


def make_batch(symbol: str, start: datetime, n: int) -> list[dict]:
    return [
        {
            "symbol": symbol,
            "asset_class": "equity",
            "timeframe": "1Min",
            "timestamp": start + timedelta(minutes=i),
            "open": 100.0 + i,
            "high": 100.5 + i,
            "low": 99.5 + i,
            "close": 100.2 + i,
            "volume": 1000.0,
            "trade_count": 20,
            "vwap": 100.1 + i,
        }
        for i in range(n)
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows-per-batch", type=int, default=2000)
    parser.add_argument("--batches", type=int, default=50)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="mdd_bench_"))
    try:
        db = MarketDataDB(DBConfig(root=tmp))
        symbols = [f"SYM{w}" for w in range(args.workers)]

        jobs = []
        for b in range(args.batches):
            for w, sym in enumerate(symbols):
                start = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=b)
                provider = "alpaca" if w % 2 == 0 else "lse"
                source = "live" if w % 3 == 0 else "backfill"
                jobs.append((sym, start, provider, source))

        def run(job):
            sym, start, provider, source = job
            batch = make_batch(sym, start, args.rows_per_batch)
            return db.write(Dataset.BARS, batch, provider=provider, source=source)

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(run, jobs))
        elapsed = time.perf_counter() - t0

        total_rows = sum(results)
        print(f"Wrote {total_rows:,} rows across {len(jobs)} concurrent batches in {elapsed:.2f}s")
        print(f"Throughput: {total_rows / elapsed:,.0f} rows/sec ({args.workers} workers)")

        t0 = time.perf_counter()
        compacted = db.compact(Dataset.BARS)
        elapsed = time.perf_counter() - t0
        print(f"Compacted {compacted:,} rows in {elapsed:.2f}s ({compacted / elapsed:,.0f} rows/sec)")

        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
