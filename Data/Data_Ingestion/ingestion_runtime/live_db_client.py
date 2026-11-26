"""Optional HTTP sink: buffers live_ingestion MarketDataEvents and batch-POSTs
them to the market-data-db service's REST API.

Mirrors market_data_db/adapters/live_adapter.py's record shapes (that
module, in the separately-deployed Database component, is the source of
truth for the wire schema) without importing market_data_db, so this image
has zero dependency on that package or its heavier transitive dependencies.

No changes to live_ingestion are required: this subscribes to the engine's
existing `subscribe()` callback API, exactly like the Database team's own
(in-process) LiveSink adapter does.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

import httpx

logger = logging.getLogger("live_ingestion.platform.db_sink")

_BAR_EVENT_TIMEFRAME = {"bar": "1Min", "updated_bar": "1Min", "daily_bar": "1Day"}


def _event_type_name(event: Any) -> str:
    et = event.type
    return et.value if hasattr(et, "value") else str(et)


def _trade_to_record(payload: Any) -> dict:
    return {
        "symbol": payload.symbol, "asset_class": "equity", "timestamp": payload.timestamp.isoformat(),
        "price": payload.price, "size": payload.size, "exchange": payload.exchange,
        "trade_id": str(payload.trade_id) if payload.trade_id is not None else None,
        "conditions": ",".join(payload.conditions) if payload.conditions else None,
        "tape": getattr(payload, "tape", None),
    }


def _quote_to_record(payload: Any) -> dict:
    return {
        "symbol": payload.symbol, "asset_class": "equity", "timestamp": payload.timestamp.isoformat(),
        "bid_price": payload.bid_price, "bid_size": payload.bid_size, "ask_price": payload.ask_price,
        "ask_size": payload.ask_size, "bid_exchange": payload.bid_exchange, "ask_exchange": payload.ask_exchange,
        "conditions": ",".join(payload.conditions) if getattr(payload, "conditions", None) else None,
    }


def _bar_to_record(payload: Any, timeframe: str) -> dict:
    return {
        "symbol": payload.symbol, "asset_class": "equity", "timeframe": timeframe,
        "timestamp": payload.timestamp.isoformat(), "open": payload.open, "high": payload.high,
        "low": payload.low, "close": payload.close, "volume": payload.volume,
        "trade_count": payload.trade_count, "vwap": payload.vwap,
    }


class LiveHttpSink:
    """Buffers MarketDataEvent objects and batch-writes them over HTTP.

    handle_event() is cheap (an in-memory append) so it's safe to use
    directly as the engine's `subscribe()` callback, which runs synchronously
    inline per event. Call `await flush()` periodically and once more on
    shutdown.
    """

    def __init__(self, base_url: str, api_key: str, *, provider: str = "alpaca", batch_size: int = 500,
                 max_retries: int = 3, retry_base_delay: float = 1.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, headers={"X-API-Key": api_key}, timeout=30.0)
        self.provider = provider
        self.batch_size = batch_size
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._lock = threading.Lock()
        self._buffers: dict[str, list[dict]] = {"trades": [], "quotes": [], "bars": []}
        self._pending_flush: set[str] = set()

    def handle_event(self, event: Any) -> None:
        kind = _event_type_name(event)
        if kind == "trade":
            record, dataset = _trade_to_record(event.payload), "trades"
        elif kind == "quote":
            record, dataset = _quote_to_record(event.payload), "quotes"
        elif kind in _BAR_EVENT_TIMEFRAME:
            record, dataset = _bar_to_record(event.payload, _BAR_EVENT_TIMEFRAME[kind]), "bars"
        else:
            return  # e.g. "status" events carry no normalized market data

        with self._lock:
            buf = self._buffers[dataset]
            buf.append(record)
            if len(buf) >= self.batch_size:
                self._pending_flush.add(dataset)

    async def flush(self) -> dict[str, int]:
        results: dict[str, int] = {}
        for dataset in list(self._buffers):
            with self._lock:
                records, self._buffers[dataset] = self._buffers[dataset], []
                self._pending_flush.discard(dataset)
            if not records:
                continue
            results[dataset] = await self._write(dataset, records)
        return results

    async def _write(self, dataset: str, records: list[dict]) -> int:
        from asyncio import sleep

        attempt = 0
        while True:
            try:
                response = await self._client.post(
                    f"/v1/datasets/{dataset}/write",
                    json={"provider": self.provider, "source": "live", "records": records},
                )
                response.raise_for_status()
                return response.json().get("rows_written", len(records))
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                attempt += 1
                if attempt > self._max_retries:
                    logger.error("dropping %d %s records after %d failed attempts: %s",
                                 len(records), dataset, attempt, exc)
                    return 0
                await sleep(self._retry_base_delay * (2 ** (attempt - 1)))

    async def close(self) -> None:
        await self._client.aclose()
