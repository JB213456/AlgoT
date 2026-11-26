import tempfile
from pathlib import Path

import pytest

from backfill_engine import db
from backfill_engine.models import Bar


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = db.connect(str(Path(tmp) / "test.sqlite3"))
        yield c
        c.close()


def _bar(ts, symbol="AAPL", provider="alpaca", feed="iex", resolution="1Day"):
    return Bar(provider=provider, feed=feed, symbol=symbol, resolution=resolution, timestamp=ts,
               open=1, high=2, low=0.5, close=1.5, volume=100)


def test_upsert_bars_and_idempotent(conn):
    bars = [_bar("2024-01-01T00:00:00Z"), _bar("2024-01-02T00:00:00Z")]
    n = db.upsert_bars(conn, bars)
    assert n == 2
    # Re-inserting the same rows (dedup via PRIMARY KEY) must not create duplicates.
    db.upsert_bars(conn, bars)
    count = conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
    assert count == 2


def test_coverage_round_trip(conn):
    assert db.get_coverage(conn, "alpaca", "alpaca", "bars", "AAPL", "1Day") is None
    db.update_coverage(conn, "alpaca", "alpaca", "bars", "AAPL", "1Day", "2024-01-01", "2024-01-31", 20, "now")
    cov = db.get_coverage(conn, "alpaca", "alpaca", "bars", "AAPL", "1Day")
    assert cov == ("2024-01-01", "2024-01-31")
    # Extending coverage widens the window rather than overwriting it.
    db.update_coverage(conn, "alpaca", "alpaca", "bars", "AAPL", "1Day", "2024-02-01", "2024-02-28", 15, "later")
    cov = db.get_coverage(conn, "alpaca", "alpaca", "bars", "AAPL", "1Day")
    assert cov == ("2024-01-01", "2024-02-28")


def test_coverage_report(conn):
    db.update_coverage(conn, "alpaca", "alpaca", "bars", "AAPL", "1Day", "2024-01-01", "2024-01-31", 20, "now")
    rows = db.coverage_report(conn, "AAPL")
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["rows_fetched"] == 20


def test_provider_and_feed_distinction_not_collapsed(conn):
    a = _bar("2024-01-01T00:00:00Z", provider="alpaca", feed="iex")
    b = _bar("2024-01-01T00:00:00Z", provider="lse", feed="us_stocks")
    db.upsert_bars(conn, [a, b])
    count = conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
    assert count == 2
