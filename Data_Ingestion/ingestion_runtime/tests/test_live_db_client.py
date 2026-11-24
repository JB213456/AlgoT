from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

import pytest
import respx
from httpx import Response

from ingestion_runtime.live_db_client import LiveHttpSink


class _EventType(str, Enum):
    TRADE = "trade"
    QUOTE = "quote"
    BAR = "bar"
    STATUS = "status"


@dataclass
class _Trade:
    symbol: str
    timestamp: datetime
    price: float
    size: int
    exchange: str = "V"
    trade_id: int = 1
    conditions: tuple = ()
    tape: str = "C"


@dataclass
class _Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_count: int = 10
    vwap: float = 1.1


@dataclass
class _Event:
    type: _EventType
    payload: object


def _ts():
    return datetime(2024, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_handle_event_buffers_until_batch_size():
    sink = LiveHttpSink("http://market-data-db:8000", "secret", batch_size=2)
    try:
        trade = _Trade(symbol="AAPL", timestamp=_ts(), price=100.0, size=10)
        sink.handle_event(_Event(_EventType.TRADE, trade))
        assert len(sink._buffers["trades"]) == 1
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_flush_posts_buffered_trades():
    sink = LiveHttpSink("http://market-data-db:8000", "secret")
    try:
        with respx.mock(base_url="http://market-data-db:8000") as mock:
            route = mock.post("/v1/datasets/trades/write").mock(return_value=Response(200, json={"rows_written": 1}))
            trade = _Trade(symbol="AAPL", timestamp=_ts(), price=100.0, size=10)
            sink.handle_event(_Event(_EventType.TRADE, trade))
            result = await sink.flush()
            assert result == {"trades": 1}
            assert route.call_count == 1
            assert route.calls.last.request.headers["X-API-Key"] == "secret"
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_bar_event_maps_to_bars_dataset_with_timeframe():
    sink = LiveHttpSink("http://market-data-db:8000", "secret")
    try:
        with respx.mock(base_url="http://market-data-db:8000") as mock:
            mock.post("/v1/datasets/bars/write").mock(return_value=Response(200, json={"rows_written": 1}))
            bar = _Bar(symbol="AAPL", timestamp=_ts(), open=1, high=2, low=0.5, close=1.5, volume=100)
            sink.handle_event(_Event(_EventType.BAR, bar))
            await sink.flush()
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_status_event_is_ignored():
    sink = LiveHttpSink("http://market-data-db:8000", "secret")
    try:
        sink.handle_event(_Event(_EventType.STATUS, object()))
        assert all(len(v) == 0 for v in sink._buffers.values())
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_flush_with_empty_buffers_makes_no_requests():
    sink = LiveHttpSink("http://market-data-db:8000", "secret")
    try:
        with respx.mock(base_url="http://market-data-db:8000") as mock:
            result = await sink.flush()
            assert result == {}
            assert len(mock.calls) == 0
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_write_drops_batch_after_exhausting_retries():
    sink = LiveHttpSink("http://market-data-db:8000", "secret", max_retries=1, retry_base_delay=0.01)
    try:
        with respx.mock(base_url="http://market-data-db:8000") as mock:
            mock.post("/v1/datasets/trades/write").mock(return_value=Response(500))
            trade = _Trade(symbol="AAPL", timestamp=_ts(), price=100.0, size=10)
            sink.handle_event(_Event(_EventType.TRADE, trade))
            result = await sink.flush()
            assert result == {"trades": 0}
    finally:
        await sink.close()
