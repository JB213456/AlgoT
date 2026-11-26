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


def _trade(i, ts, symbol="AAPL"):
    return {
        "symbol": symbol,
        "asset_class": "equity",
        "timestamp": ts,
        "price": 100.0 + i,
        "size": 1.0,
        "exchange": "X",
        "trade_id": f"t{i}-{symbol}",
        "conditions": None,
        "tape": "C",
    }


def test_write_accepts_mixed_subsecond_precision(db):
    """A batch mixing whole-second and sub-second ISO timestamps must write.

    pandas infers one strptime format from the first element unless told to
    parse ISO8601 per element, so this used to fail for the whole batch
    depending on which row happened to come first.
    """
    stamps = [
        "2026-02-02T14:30:00+00:00",         # whole second first
        "2026-02-02T14:30:00.100000+00:00",  # then sub-second
        "2026-02-02T14:30:01.5+00:00",       # then a different precision
        "2026-02-02T14:30:02Z",              # then Z-suffixed whole second
    ]
    records = [_trade(i, ts) for i, ts in enumerate(stamps)]
    assert db.write(Dataset.TRADES, records, provider="p", source="backfill") == 4

    out = db.read(Dataset.TRADES, symbols=["AAPL"], dedup=None)
    assert len(out) == 4
    assert sorted(int(t.microsecond) for t in out["timestamp"]) == [0, 0, 100000, 500000]


def test_write_accepts_subsecond_first(db):
    """Order must not matter: sub-second first, whole second later."""
    records = [
        _trade(0, "2026-02-02T14:30:00.250000+00:00", symbol="MSFT"),
        _trade(1, "2026-02-02T14:30:01+00:00", symbol="MSFT"),
    ]
    assert db.write(Dataset.TRADES, records, provider="p", source="backfill") == 2
