import json
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

from market_data_db.adapters.backfill_adapter import ingest_record
from market_data_db.adapters.live_adapter import LiveSink
from market_data_db.schema import Dataset


# --- minimal stand-ins for backfill_engine.models dataclasses, matched by name
# (the adapter dispatches on type(obj).__name__) so these tests carry no import
# dependency on the backfill_engine package. ---


@dataclass
class Bar:
    provider: str
    feed: str
    symbol: str
    resolution: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: Optional[int] = None
    vwap: Optional[float] = None
    adjustment: str = "raw"


@dataclass
class CorporateAction:
    provider: str
    feed: str
    symbol: str
    action_type: str
    ex_date: str
    declared_date: Optional[str] = None
    record_date: Optional[str] = None
    pay_date: Optional[str] = None
    ratio: Optional[float] = None
    cash_amount: Optional[float] = None


@dataclass
class FundamentalRecord:
    provider: str
    feed: Optional[str]
    symbol: str
    as_of: str
    statement_type: str
    period: str
    fields_json: str


def _live_trade_event():
    payload = SimpleNamespace(
        symbol="AAPL",
        timestamp=datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
        price=190.5,
        size=100,
        exchange="V",
        trade_id=12345,
        conditions=("@",),
        tape="C",
    )
    return SimpleNamespace(type=SimpleNamespace(value="trade"), symbol="AAPL", payload=payload)


def test_live_sink_buffers_and_flushes(db):
    sink = LiveSink(db, provider="alpaca", batch_size=1000)
    for _ in range(3):
        sink.handle_event(_live_trade_event())

    assert db.row_count(Dataset.TRADES, dedup=None) == 0  # still buffered
    flushed = sink.flush()
    assert flushed[Dataset.TRADES] == 3
    assert db.row_count(Dataset.TRADES, dedup=None) == 3


def test_live_sink_auto_flushes_at_batch_size(db):
    sink = LiveSink(db, provider="alpaca", batch_size=2)
    for _ in range(5):
        sink.handle_event(_live_trade_event())
    assert db.row_count(Dataset.TRADES, dedup=None) == 4  # two batches auto-flushed
    sink.flush()
    assert db.row_count(Dataset.TRADES, dedup=None) == 5


def test_ingest_record_bar(db):
    bar = Bar(
        provider="alpaca",
        feed="iex",
        symbol="AAPL",
        resolution="1Day",
        timestamp="2024-01-02T00:00:00Z",
        open=100.0,
        high=105.0,
        low=99.0,
        close=104.0,
        volume=1_000_000.0,
        trade_count=5000,
        vwap=102.0,
    )
    rows = ingest_record(db, bar)
    assert rows == 1
    out = db.read(Dataset.BARS, symbols=["AAPL"])
    assert len(out) == 1
    assert out.iloc[0]["provider"] == "alpaca_iex"


def test_ingest_record_corporate_action(db):
    action = CorporateAction(
        provider="alpaca",
        feed="iex",
        symbol="AAPL",
        action_type="split",
        ex_date="2024-06-10",
        ratio=4.0,
    )
    rows = ingest_record(db, action)
    assert rows == 1
    out = db.read(Dataset.CORPORATE_ACTIONS, symbols=["AAPL"])
    assert len(out) == 1
    assert out.iloc[0]["ratio"] == 4.0


def test_ingest_record_fundamental_income_statement(db):
    rec = FundamentalRecord(
        provider="lse",
        feed=None,
        symbol="AAPL",
        as_of="2024-02-01T00:00:00Z",
        statement_type="income",
        period="2024-Q1",
        fields_json=json.dumps({"revenue": 1.2e9, "net_income": 3.4e8}),
    )
    rows = ingest_record(db, rec)
    assert rows == 2
    out = db.read(Dataset.FINANCIAL_STATEMENTS, symbols=["AAPL"])
    assert len(out) == 2
    assert set(out["line_item"]) == {"revenue", "net_income"}
