from datetime import datetime, timezone

from market_data_db.schema import Dataset


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


def test_idempotent_rewrite_is_deduped_on_read(db):
    t0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    row = _bar("AAPL", t0, 100.0)
    db.write(Dataset.BARS, [row], provider="alpaca", source="backfill")
    db.write(Dataset.BARS, [row], provider="alpaca", source="backfill")  # re-run same batch

    assert db.row_count(Dataset.BARS, dedup=None) == 2  # both physical rows exist
    assert db.row_count(Dataset.BARS, dedup="exact") == 1  # but dedup collapses them

    report = db.check_integrity(Dataset.BARS)
    assert report.duplicate_keys == 1
    assert report.duplicate_rows == 2


def test_compaction_removes_physical_duplicates(db):
    t0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    row = _bar("AAPL", t0, 100.0)
    db.write(Dataset.BARS, [row], provider="alpaca", source="backfill")
    db.write(Dataset.BARS, [row], provider="alpaca", source="backfill")

    compacted_rows = db.compact(Dataset.BARS)
    assert compacted_rows == 1

    assert db.row_count(Dataset.BARS, dedup=None) == 1
    report = db.check_integrity(Dataset.BARS)
    assert report.duplicate_keys == 0


def test_later_ingestion_corrects_earlier_value(db):
    t0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    db.write(Dataset.BARS, [_bar("AAPL", t0, 100.0)], provider="alpaca", source="backfill")
    db.write(Dataset.BARS, [_bar("AAPL", t0, 999.0)], provider="alpaca", source="backfill")

    out = db.read(Dataset.BARS, symbols=["AAPL"], dedup="exact")
    assert len(out) == 1
    assert out.iloc[0]["close"] == 999.0  # most recently ingested wins
