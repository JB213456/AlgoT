"""REST polling to supplement the websocket feed.

The free IEX websocket feed already pushes trades, quotes and minute bars, so
REST polling here is used only where it adds value on the free tier: a
periodic latest-snapshot cross-check per symbol, which lets the engine notice
a silently stalled websocket subscription and fills gaps if one occurs.
Requests are throttled by an AsyncTokenBucket to stay within Alpaca's free
market-data rate limit.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest

from .config import Settings
from .health import ConnectionHealth
from .rate_limiter import AsyncTokenBucket

logger = logging.getLogger("live_ingestion.rest_poller")

OnSnapshot = Callable[[str, object], Awaitable[None]]


class RestPoller:
    def __init__(
        self,
        settings: Settings,
        health: ConnectionHealth,
        on_trade: OnSnapshot,
        on_quote: OnSnapshot,
        on_bar: OnSnapshot,
        client: StockHistoricalDataClient | None = None,
    ) -> None:
        self._settings = settings
        self._health = health
        self._on_trade = on_trade
        self._on_quote = on_quote
        self._on_bar = on_bar
        self._client = client or StockHistoricalDataClient(
            settings.alpaca_api_key, settings.alpaca_secret_key
        )
        self._limiter = AsyncTokenBucket(settings.rest_rate_limit_per_minute)

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self._settings.rest_polling_enabled:
            self._health.mark_stopped()
            return
        self._health.mark_connecting()
        request = StockSnapshotRequest(
            symbol_or_symbols=list(self._settings.symbols),
            feed=DataFeed(self._settings.data_feed),
        )
        while not stop_event.is_set():
            try:
                await self._limiter.acquire()
                snapshots = await asyncio.to_thread(self._client.get_stock_snapshot, request)
                self._health.mark_message()
                for symbol, snapshot in snapshots.items():
                    await self._emit(symbol, snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._health.mark_error(exc)
                logger.exception("REST poll failed")
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._settings.rest_poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass
        self._health.mark_stopped()

    async def _emit(self, symbol: str, snapshot) -> None:
        if getattr(snapshot, "latest_trade", None) is not None:
            await self._on_trade(symbol, snapshot.latest_trade)
        if getattr(snapshot, "latest_quote", None) is not None:
            await self._on_quote(symbol, snapshot.latest_quote)
        if getattr(snapshot, "minute_bar", None) is not None:
            await self._on_bar(symbol, snapshot.minute_bar)
