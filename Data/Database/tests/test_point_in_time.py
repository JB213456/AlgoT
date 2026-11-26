import time
from datetime import datetime, timezone

from market_data_db.schema import Dataset
from market_data_db.timestamps import now_utc


def _bar(symbol, ts, close):
    return {
        "symbol": symbol,
        "asset_class": "equity",
        "timeframe": "1Min",
        "timestamp": ts,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 100.0,
        "trade_count": 10,
        "vwap": close,
    }


def test_point_in_time_sees_only_data_ingested_by_then(db):
    t0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)

    db.write(Dataset.BARS, [_bar("AAPL", t0, 100.0)], provider="alpaca", source="backfill")
    cutoff = now_utc()
    time.sleep(0.05)
    db.write(Dataset.BARS, [_bar("AAPL", t0, 999.0)], provider="alpaca", source="backfill")  # a later correction

    as_of_before_correction = db.point_in_time(Dataset.BARS, cutoff, symbols=["AAPL"])
    assert len(as_of_before_correction) == 1
    assert as_of_before_correction.iloc[0]["close"] == 100.0

    as_of_now = db.point_in_time(Dataset.BARS, now_utc(), symbols=["AAPL"])
    assert len(as_of_now) == 1
    assert as_of_now.iloc[0]["close"] == 999.0
