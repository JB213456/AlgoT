from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from market_data_db.schema import Dataset


def _write_one_symbol(db, symbol, n, provider, source):
    t0 = datetime(2024, 3, 1, tzinfo=timezone.utc)
    rows = [
        {
            "symbol": symbol,
            "asset_class": "equity",
            "timeframe": "1Min",
            "timestamp": t0 + timedelta(minutes=i),
            "open": float(i),
            "high": float(i),
            "low": float(i),
            "close": float(i),
            "volume": 1.0,
            "trade_count": 1,
            "vwap": float(i),
        }
        for i in range(n)
    ]
    return db.write(Dataset.BARS, rows, provider=provider, source=source)


def test_concurrent_writers_do_not_lose_rows(db):
    jobs = [
        ("AAPL", 200, "alpaca", "live"),
        ("AAPL", 200, "alpaca", "backfill"),
        ("MSFT", 200, "alpaca", "live"),
        ("MSFT", 200, "lse", "backfill"),
        ("GOOG", 200, "alpaca", "live"),
        ("GOOG", 200, "alpaca", "backfill"),
        ("TSLA", 200, "alpaca", "live"),
        ("TSLA", 200, "lse", "backfill"),
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda j: _write_one_symbol(db, *j), jobs))

    assert sum(results) == sum(n for _, n, _, _ in jobs)
    assert db.row_count(Dataset.BARS, dedup=None) == sum(n for _, n, _, _ in jobs)

    # Compacting every partition afterwards must not change the (deduped) totals,
    # and must not collide between concurrently-touched partitions.
    db.compact(Dataset.BARS)
    assert db.row_count(Dataset.BARS, dedup=None) == sum(n for _, n, _, _ in jobs)
