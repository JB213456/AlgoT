"""Adapter for live_ingestion.MarketDataEngine (Alpaca live feed).

No changes to live_ingestion are required. Wire it up with either:

    sink = LiveSink(db, provider="alpaca")
    engine.subscribe(sink.handle_event)
    ...
    sink.flush()   # periodically, and on shutdown

or drive it directly from the engine's async generator:

    async for event in engine.events():
        sink.handle_event(event)

This module intentionally has no import-time dependency on the `live_ingestion`
package (duck-typed on attribute names) so this database stays usable standalone.
"""
from __future__ import annotations

import threading
from typing import Any

from ..db import MarketDataDB
from ..schema import Dataset

_BAR_EVENT_TIMEFRAME = {
    "bar": "1Min",
    "updated_bar": "1Min",
    "daily_bar": "1Day",
}


def _event_type_name(event: Any) -> str:
    et = event.type
    return et.value if hasattr(et, "value") else str(et)


def trade_to_record(payload: Any) -> dict:
    return {
        "symbol": payload.symbol,
        "asset_class": "equity",
        "timestamp": payload.timestamp,
        "price": payload.price,
        "size": payload.size,
        "exchange": payload.exchange,
        "trade_id": str(payload.trade_id) if payload.trade_id is not None else None,
        "conditions": ",".join(payload.conditions) if payload.conditions else None,
        "tape": getattr(payload, "tape", None),
    }


def quote_to_record(payload: Any) -> dict:
    return {
        "symbol": payload.symbol,
        "asset_class": "equity",
        "timestamp": payload.timestamp,
        "bid_price": payload.bid_price,
        "bid_size": payload.bid_size,
        "ask_price": payload.ask_price,
        "ask_size": payload.ask_size,
        "bid_exchange": payload.bid_exchange,
        "ask_exchange": payload.ask_exchange,
        "conditions": ",".join(payload.conditions) if getattr(payload, "conditions", None) else None,
    }


def bar_to_record(payload: Any, timeframe: str) -> dict:
    return {
        "symbol": payload.symbol,
        "asset_class": "equity",
        "timeframe": timeframe,
        "timestamp": payload.timestamp,
        "open": payload.open,
        "high": payload.high,
        "low": payload.low,
        "close": payload.close,
        "volume": payload.volume,
        "trade_count": payload.trade_count,
        "vwap": payload.vwap,
    }


class LiveSink:
    """Buffers MarketDataEvent objects from live_ingestion and batch-writes them.

    handle_event() is cheap (an in-memory append) so it is safe to use directly as
    the engine's `subscribe()` callback, which runs synchronously inline per event.
    Call flush() periodically (e.g. every few seconds from a timer task) and once
    more on shutdown.
    """

    def __init__(self, db: MarketDataDB, *, provider: str = "alpaca", batch_size: int = 1000):
        self.db = db
        self.provider = provider
        self.batch_size = batch_size
        self._lock = threading.Lock()
        self._buffers: dict[Dataset, list[dict]] = {Dataset.TRADES: [], Dataset.QUOTES: [], Dataset.BARS: []}

    def handle_event(self, event: Any) -> None:
        kind = _event_type_name(event)
        if kind == "trade":
            record = trade_to_record(event.payload)
            dataset = Dataset.TRADES
        elif kind == "quote":
            record = quote_to_record(event.payload)
            dataset = Dataset.QUOTES
        elif kind in _BAR_EVENT_TIMEFRAME:
            record = bar_to_record(event.payload, _BAR_EVENT_TIMEFRAME[kind])
            dataset = Dataset.BARS
        else:
            return  # e.g. "status" events carry no normalized market data

        flush_needed = False
        with self._lock:
            buf = self._buffers[dataset]
            buf.append(record)
            if len(buf) >= self.batch_size:
                flush_needed = True
        if flush_needed:
            self._flush_one(dataset)

    def _flush_one(self, dataset: Dataset) -> int:
        with self._lock:
            records, self._buffers[dataset] = self._buffers[dataset], []
        if not records:
            return 0
        return self.db.write(dataset, records, provider=self.provider, source="live")

    def flush(self) -> dict[Dataset, int]:
        return {ds: self._flush_one(ds) for ds in list(self._buffers)}
