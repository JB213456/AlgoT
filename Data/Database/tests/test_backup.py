from datetime import datetime, timezone

from market_data_db import DBConfig, MarketDataDB
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


def test_backup_and_restore_roundtrip(db, tmp_path):
    t0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    db.write(Dataset.BARS, [_bar("AAPL", t0)], provider="alpaca", source="backfill")
    db.compact(Dataset.BARS)

    archive = db.backup(label="test")
    assert archive.exists()

    restore_root = tmp_path / "restored"
    restored = MarketDataDB(DBConfig(root=restore_root))
    restored.restore(archive, overwrite=True)
    restored.close()

    reopened = MarketDataDB(DBConfig(root=restore_root))
    out = reopened.read(Dataset.BARS, symbols=["AAPL"])
    assert len(out) == 1
    assert out.iloc[0]["close"] == 1.0
    cov = reopened.coverage(Dataset.BARS, symbol="AAPL")
    assert len(cov) == 1
    reopened.close()
