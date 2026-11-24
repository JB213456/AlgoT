from datetime import datetime, timedelta, timezone

from market_data_db.schema import Dataset


def _bar(symbol, ts):
    return {
        "symbol": symbol,
        "asset_class": "equity",
        "timeframe": "1Min",
        "timestamp": ts,
        "open": 1.0,
        "high": 1.0,
        "low": 1.0,
        "close": 1.0,
        "volume": 1.0,
        "trade_count": 1,
        "vwap": 1.0,
    }


def test_find_gaps_detects_missing_bar(db):
    t0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    minutes = [0, 1, 2, 10, 11]  # a gap between minute 2 and minute 10
    rows = [_bar("AAPL", t0 + timedelta(minutes=m)) for m in minutes]
    db.write(Dataset.BARS, rows, provider="alpaca", source="backfill")

    report = db.find_gaps(Dataset.BARS, "AAPL", max_gap_seconds=120)
    assert len(report.gaps) == 1
    assert report.gaps.iloc[0]["gap_seconds"] == 8 * 60


def test_coverage_tracks_min_max_and_row_count(db):
    t0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    rows = [_bar("AAPL", t0 + timedelta(minutes=i)) for i in range(5)]
    db.write(Dataset.BARS, rows, provider="alpaca", source="backfill")

    cov = db.coverage(Dataset.BARS, symbol="AAPL")
    assert len(cov) == 1
    assert cov.iloc[0]["row_count"] == 5
    assert cov.iloc[0]["min_ts"].to_pydatetime() == t0
