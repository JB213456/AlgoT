import asyncio
import dataclasses

import pytest

from live_ingestion.engine import MarketDataEngine
from live_ingestion.models import EventType, Source
from .conftest import make_raw_bar, make_raw_quote, make_raw_trade


@pytest.mark.asyncio
async def test_trade_flows_through_to_queue_and_cache(settings):
    engine = MarketDataEngine(settings)
    await engine._handle_trade(make_raw_trade(), Source.WEBSOCKET)

    event = await asyncio.wait_for(engine._queue.get(), timeout=1)
    assert event.type == EventType.TRADE
    assert event.symbol == "AAPL"
    assert engine.latest("AAPL")["trade"].price == 100.0


@pytest.mark.asyncio
async def test_duplicate_trade_is_dropped(settings):
    engine = MarketDataEngine(settings)
    raw = make_raw_trade()
    await engine._handle_trade(raw, Source.WEBSOCKET)
    await engine._handle_trade(raw, Source.WEBSOCKET)  # identical trade_id -> dup
    assert engine._queue.qsize() == 1


@pytest.mark.asyncio
async def test_invalid_trade_increments_validation_errors(settings):
    engine = MarketDataEngine(settings)
    await engine._handle_trade(make_raw_trade(price=-1), Source.WEBSOCKET)
    assert engine._queue.qsize() == 0
    assert engine._validation_errors == 1


@pytest.mark.asyncio
async def test_quote_and_bar_update_latest_cache(settings):
    engine = MarketDataEngine(settings)
    await engine._handle_quote(make_raw_quote(), Source.WEBSOCKET)
    await engine._handle_bar(make_raw_bar(), Source.WEBSOCKET, updated=False, daily=False)

    latest = engine.latest("AAPL")
    assert latest["quote"].ask_price == 100.0
    assert latest["bar"].close == 100.5


@pytest.mark.asyncio
async def test_registered_callback_is_invoked(settings):
    engine = MarketDataEngine(settings)
    received = []
    engine.subscribe(lambda event: received.append(event))

    await engine._handle_trade(make_raw_trade(), Source.WEBSOCKET)
    assert len(received) == 1
    assert received[0].symbol == "AAPL"


@pytest.mark.asyncio
async def test_queue_drops_oldest_when_full(settings):
    small_queue_settings = dataclasses.replace(settings, queue_max_size=2)
    engine = MarketDataEngine(small_queue_settings)
    for i in range(5):
        await engine._handle_trade(make_raw_trade(trade_id=i, price=100 + i), Source.WEBSOCKET)

    assert engine._queue.qsize() == 2
    assert engine._dropped_events == 3
