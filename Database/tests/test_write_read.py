from datetime import datetime, timedelta, timezone

from market_data_db.schema import Dataset


def _bar(symbol, ts, close, timeframe="1Min"):
    return {
        "symbol": symbol,
        "asset_class": "equity",
        "timeframe": timeframe,
        "timestamp": ts,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 100.0,
        "trade_count": 10,
        "vwap": close,
    }


def test_write_then_read_roundtrip(db):
    t0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    rows = [_bar("AAPL", t0 + timedelta(minutes=i), 100.0 + i) for i in range(5)]
    written = db.write(Dataset.BARS, rows, provider="alpaca", source="backfill")
    assert written == 5

    out = db.read(Dataset.BARS, symbols=["AAPL"])
    assert len(out) == 5
    assert sorted(out["close"].tolist()) == [100.0, 101.0, 102.0, 103.0, 104.0]


def test_live_overrides_backfill_in_canonical_read(db):
    t0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    db.write(Dataset.BARS, [_bar("AAPL", t0, 100.0)], provider="lse", source="backfill")
    db.write(Dataset.BARS, [_bar("AAPL", t0, 101.0)], provider="alpaca", source="live")

    canonical = db.read(Dataset.BARS, symbols=["AAPL"], dedup="canonical")
    assert len(canonical) == 1
    assert canonical.iloc[0]["close"] == 101.0
    assert canonical.iloc[0]["source"] == "live"

    exact = db.read(Dataset.BARS, symbols=["AAPL"], dedup="exact")
    assert len(exact) == 2  # both providers' rows preserved underneath


def test_get_latest(db):
    t0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    rows = [_bar("MSFT", t0 + timedelta(minutes=i), 50.0 + i) for i in range(3)]
    db.write(Dataset.BARS, rows, provider="alpaca", source="backfill")

    latest = db.get_latest(Dataset.BARS, "MSFT")
    assert latest is not None
    assert latest["close"] == 52.0
