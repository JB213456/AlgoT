import types
from datetime import datetime, timezone

import pytest

from live_ingestion.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        alpaca_api_key="test-key",
        alpaca_secret_key="test-secret",
        data_feed="iex",
        symbols=("AAPL", "MSFT"),
        subscriptions=frozenset({"trades", "quotes", "bars"}),
        rest_poll_interval_seconds=0,  # disabled by default in tests
        queue_max_size=100,
    )


def make_raw_trade(symbol="AAPL", price=100.0, size=10, trade_id=1, ts=None):
    return types.SimpleNamespace(
        symbol=symbol,
        timestamp=ts or datetime(2024, 1, 1, tzinfo=timezone.utc),
        price=price,
        size=size,
        exchange="V",
        id=trade_id,
        conditions=["@"],
        tape="C",
    )


def make_raw_quote(symbol="AAPL", bid=99.0, ask=100.0, ts=None):
    return types.SimpleNamespace(
        symbol=symbol,
        timestamp=ts or datetime(2024, 1, 1, tzinfo=timezone.utc),
        bid_price=bid,
        bid_size=1,
        ask_price=ask,
        ask_size=2,
        bid_exchange="V",
        ask_exchange="V",
        conditions=["R"],
        tape="C",
    )


def make_raw_bar(symbol="AAPL", o=100.0, h=101.0, l=99.0, c=100.5, v=1000, ts=None):
    return types.SimpleNamespace(
        symbol=symbol,
        timestamp=ts or datetime(2024, 1, 1, tzinfo=timezone.utc),
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
        trade_count=10,
        vwap=100.1,
    )
