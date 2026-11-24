"""The unified live market data ingestion engine.

MarketDataEngine wires together the websocket client, REST poller,
normalization/validation, deduplication, health monitoring, and exposes a
single interface downstream systems use to consume live data: an async
event stream, per-symbol latest-value cache, callback subscriptions, and a
health snapshot.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Union

from .config import Settings
from .dedup import Deduplicator
from .health import HealthMonitor
from .models import EventType, MarketDataEvent, Source
from .normalize import ValidationError, normalize_bar, normalize_quote, normalize_trade
from .rest_poller import RestPoller
from .ws_client import AlpacaWebSocketClient

logger = logging.getLogger("live_ingestion.engine")

EventCallback = Callable[[MarketDataEvent], Union[None, Awaitable[None]]]


class MarketDataEngine:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._queue: asyncio.Queue[MarketDataEvent] = asyncio.Queue(maxsize=settings.queue_max_size)
        self._dedup = Deduplicator()
        self._latest: Dict[str, Dict[str, Any]] = {}
        self._callbacks: List[EventCallback] = []
        self._health_monitor = HealthMonitor(log_interval_seconds=settings.health_log_interval_seconds)
        self._ws_health = self._health_monitor.register("websocket")
        self._rest_health = self._health_monitor.register("rest_poller")
        self._stop_event = asyncio.Event()
        self._tasks: List[asyncio.Task] = []
        self._dropped_events = 0
        self._validation_errors = 0

        self._ws_client = AlpacaWebSocketClient(
            settings,
            self._ws_health,
            on_trade=lambda raw: self._handle_trade(raw, Source.WEBSOCKET),
            on_quote=lambda raw: self._handle_quote(raw, Source.WEBSOCKET),
            on_bar=lambda raw: self._handle_bar(raw, Source.WEBSOCKET, updated=False, daily=False),
            on_updated_bar=lambda raw: self._handle_bar(raw, Source.WEBSOCKET, updated=True, daily=False),
            on_daily_bar=lambda raw: self._handle_bar(raw, Source.WEBSOCKET, updated=False, daily=True),
            on_status=self._handle_status,
        )
        self._rest_poller = RestPoller(
            settings,
            self._rest_health,
            on_trade=lambda _symbol, raw: self._handle_trade(raw, Source.REST),
            on_quote=lambda _symbol, raw: self._handle_quote(raw, Source.REST),
            on_bar=lambda _symbol, raw: self._handle_bar(raw, Source.REST, updated=False, daily=False),
        )

    # -- public API ---------------------------------------------------

    def subscribe(self, callback: EventCallback) -> None:
        """Registers a callback invoked (sync or async) for every normalized event."""
        self._callbacks.append(callback)

    async def events(self):
        """Async generator yielding normalized MarketDataEvents as they arrive."""
        while True:
            event = await self._queue.get()
            yield event

    def latest(self, symbol: str) -> Dict[str, Any]:
        """Returns the latest cached trade/quote/bar for a symbol."""
        return dict(self._latest.get(symbol, {}))

    def health_snapshot(self) -> dict:
        snapshot = self._health_monitor.snapshot()
        snapshot["_queue"] = {
            "size": self._queue.qsize(),
            "max_size": self._settings.queue_max_size,
            "dropped_events": self._dropped_events,
            "validation_errors": self._validation_errors,
        }
        return snapshot

    async def start(self) -> None:
        self._stop_event.clear()
        self._tasks = [
            asyncio.create_task(self._supervise_websocket(), name="ws-supervisor"),
            asyncio.create_task(self._rest_poller.run(self._stop_event), name="rest-poller"),
            asyncio.create_task(self._health_monitor.run(self._stop_event), name="health-monitor"),
        ]
        logger.info(
            "engine started: symbols=%s subscriptions=%s feed=%s",
            self._settings.symbols,
            sorted(self._settings.subscriptions),
            self._settings.data_feed,
        )

    async def stop(self) -> None:
        self._stop_event.set()
        await self._ws_client.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("engine stopped")

    async def run_forever(self) -> None:
        """Convenience: start the engine and block until cancelled."""
        await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()

    # -- websocket supervision (defense-in-depth reconnection) --------

    async def _supervise_websocket(self) -> None:
        backoff = 1.0
        max_backoff = 60.0
        while not self._stop_event.is_set():
            try:
                await self._ws_client.run()
                # _run_forever only returns on stop(); treat as a clean exit.
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "websocket client exited unexpectedly, restarting in %.1fs", backoff
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                    return  # stop was requested during backoff
                except asyncio.TimeoutError:
                    pass
                backoff = min(max_backoff, backoff * 2)

    # -- normalization / dedup / fan-out --------------------------------

    async def _handle_trade(self, raw: Any, source: Source) -> None:
        try:
            trade = normalize_trade(raw, source=source)
        except ValidationError as exc:
            self._validation_errors += 1
            logger.warning("dropping invalid trade: %s", exc)
            return
        if self._dedup.is_duplicate(f"trade:{trade.symbol}", trade.dedup_key()):
            return
        self._latest.setdefault(trade.symbol, {})["trade"] = trade
        await self._publish(EventType.TRADE, trade.symbol, trade.timestamp, trade, source)

    async def _handle_quote(self, raw: Any, source: Source) -> None:
        try:
            quote = normalize_quote(raw, source=source)
        except ValidationError as exc:
            self._validation_errors += 1
            logger.warning("dropping invalid quote: %s", exc)
            return
        if self._dedup.is_duplicate(f"quote:{quote.symbol}", quote.dedup_key()):
            return
        self._latest.setdefault(quote.symbol, {})["quote"] = quote
        await self._publish(EventType.QUOTE, quote.symbol, quote.timestamp, quote, source)

    async def _handle_bar(self, raw: Any, source: Source, updated: bool, daily: bool) -> None:
        try:
            bar = normalize_bar(raw, source=source, updated=updated, daily=daily)
        except ValidationError as exc:
            self._validation_errors += 1
            logger.warning("dropping invalid bar: %s", exc)
            return
        bucket = "daily_bar" if daily else ("updated_bar" if updated else "bar")
        if self._dedup.is_duplicate(f"{bucket}:{bar.symbol}", bar.dedup_key()):
            return
        cache_key = "daily_bar" if daily else "bar"
        self._latest.setdefault(bar.symbol, {})[cache_key] = bar
        event_type = EventType.DAILY_BAR if daily else (EventType.UPDATED_BAR if updated else EventType.BAR)
        await self._publish(event_type, bar.symbol, bar.timestamp, bar, source)

    async def _handle_status(self, raw: Any) -> None:
        symbol = getattr(raw, "symbol", "UNKNOWN")
        now = datetime.now(timezone.utc)
        await self._publish(EventType.STATUS, symbol, now, raw, Source.WEBSOCKET)

    async def _publish(
        self,
        event_type: EventType,
        symbol: str,
        timestamp: datetime,
        payload: Any,
        source: Source,
    ) -> None:
        event = MarketDataEvent(
            type=event_type,
            symbol=symbol,
            timestamp=timestamp,
            payload=payload,
            received_at=datetime.now(timezone.utc),
            source=source,
        )
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self._dropped_events += 1
                logger.warning("event queue full, dropped oldest event (total dropped=%d)", self._dropped_events)
            except asyncio.QueueEmpty:
                pass
        await self._queue.put(event)

        for callback in self._callbacks:
            try:
                result = callback(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("event callback raised")
