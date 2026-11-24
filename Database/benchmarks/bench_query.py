"""Benchmark query performance (point lookup, range scan, latest-observation,
point-in-time) against a lake with several million rows.

Usage: python benchmarks/bench_query.py [--rows-per-symbol N] [--symbols N]
"""
from __future__ import annotations

import argparse
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_data_db import DBConfig, MarketDataDB
from market_data_db.schema import Dataset


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows-per-symbol", type=int, default=200_000)
    parser.add_argument("--symbols", type=int, default=10)
    parser.add_argument("--write-batch-size", type=int, default=50_000)
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="mdd_bench_"))
    try:
        db = MarketDataDB(DBConfig(root=tmp))
        symbols = [f"SYM{i}" for i in range(args.symbols)]
        start = datetime(2020, 1, 1, tzinfo=timezone.utc)

        t0 = time.perf_counter()
        total = 0
        for sym in symbols:
            n = args.rows_per_symbol
            for off in range(0, n, args.write_batch_size):
                chunk = min(args.write_batch_size, n - off)
                batch = [
                    {
                        "symbol": sym,
                        "asset_class": "equity",
                        "timeframe": "1Min",
                        "timestamp": start + timedelta(minutes=off + i),
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.5,
                        "volume": 500.0,
                        "trade_count": 10,
                        "vwap": 100.3,
                    }
                    for i in range(chunk)
                ]
                total += db.write(Dataset.BARS, batch, provider="alpaca", source="backfill")
        load_elapsed = time.perf_counter() - t0
        print(f"Loaded {total:,} rows in {load_elapsed:.2f}s ({total / load_elapsed:,.0f} rows/sec)")

        t0 = time.perf_counter()
        db.compact(Dataset.BARS)
        print(f"Compaction: {time.perf_counter() - t0:.2f}s")

        t0 = time.perf_counter()
        _ = db.get_latest(Dataset.BARS, symbols[0])
        print(f"get_latest(): {(time.perf_counter() - t0) * 1000:.3f}ms")

        t0 = time.perf_counter()
        df = db.read(Dataset.BARS, symbols=[symbols[0]], start=start, end=start + timedelta(days=1))
        print(f"range read ({len(df):,} rows): {(time.perf_counter() - t0) * 1000:.3f}ms")

        t0 = time.perf_counter()
        df_all = db.read(Dataset.BARS, start=start, end=start + timedelta(hours=1))
        print(f"cross-symbol range read ({len(df_all):,} rows, all {args.symbols} symbols): "
              f"{(time.perf_counter() - t0) * 1000:.3f}ms")

        as_of = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        _ = db.point_in_time(Dataset.BARS, as_of, symbols=[symbols[0]])
        print(f"point_in_time(): {(time.perf_counter() - t0) * 1000:.3f}ms")

        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
