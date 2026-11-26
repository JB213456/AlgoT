"""Websocket ingestion built on alpaca-py's StockDataStream.

alpaca-py's StockDataStream already implements reconnection with backoff and
resubscription after a reconnect (see alpaca.data.live.websocket.DataStream).
We drive its internal coroutine directly (instead of its blocking `run()`,
which calls asyncio.run()) so it can live inside our own event loop alongside
the REST poller and health monitor. On top of that we add an outer supervisor
that restarts the whole client if the SDK's own loop ever gives up (e.g. a
fatal auth error), which the engine uses for defense-in-depth reconnection.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from alpaca.data.enums import DataFeed
from alpaca.data.live.stock import StockDataStream

from .config import Settings
from .health import ConnectionHealth

logger = logging.getLogger("live_ingestion.ws_client")

RawHandler = Callable[[object], Awaitable[None]]


class AlpacaWebSocketClient:
    def __init__(
        self,
        settings: Settings,
        health: ConnectionHealth,
        on_trade: RawHandler,
        on_quote: RawHandler,
        on_bar: RawHandler,
        on_updated_bar: RawHandler,
        on_daily_bar: RawHandler,
        on_status: RawHandler,
    ) -> None:
        self._settings = settings
        self._health = health
        self._stream = StockDataStream(
            settings.alpaca_api_key,
            settings.alpaca_secret_key,
            feed=DataFeed(settings.data_feed),
            data_timeout=settings.ws_data_timeout,
        )
        symbols = list(settings.symbols)
        subs = settings.subscriptions

        def make_wrapper(handler: RawHandler, label: str) -> RawHandler:
            async def _handler(data):
                try:
                    self._health.mark_message()
                    await handler(data)
                except Exception:
                    logger.exception("error handling %s message", label)

            return _handler

        if "trades" in subs and symbols:
            self._stream.subscribe_trades(make_wrapper(on_trade, "trade"), *symbols)
        if "quotes" in subs and symbols:
            self._stream.subscribe_quotes(make_wrapper(on_quote, "quote"), *symbols)
        if "bars" in subs and symbols:
            self._stream.subscribe_bars(make_wrapper(on_bar, "bar"), *symbols)
        if "updated_bars" in subs and symbols:
            self._stream.subscribe_updated_bars(make_wrapper(on_updated_bar, "updated_bar"), *symbols)
        if "daily_bars" in subs and symbols:
            self._stream.subscribe_daily_bars(make_wrapper(on_daily_bar, "daily_bar"), *symbols)
        if "statuses" in subs and symbols:
            self._stream.subscribe_trading_statuses(make_wrapper(on_status, "status"), *symbols)

        # The SDK only exposes connection state via log lines; wrap its
        # internal connect+auth step so health reflects the real transport
        # state (handshake + auth + subscribe) rather than only "we've seen a
        # market data message", which can legitimately never happen outside
        # trading hours even though the connection is healthy.
        original_start_ws = self._stream._start_ws

        async def _start_ws_with_health():
            await original_start_ws()
            self._health.mark_connected()

        self._stream._start_ws = _start_ws_with_health  # noqa: SLF001

    async def run(self) -> None:
        """Runs the SDK's reconnecting event loop until cancelled or fatally errored."""
        self._health.mark_connecting()
        try:
            await self._stream._run_forever()  # noqa: SLF001 - documented async entrypoint
        except asyncio.CancelledError:
            self._health.mark_stopped()
            raise
        except Exception as exc:
            self._health.mark_error(exc)
            raise

    async def close(self) -> None:
        try:
            # stop() marks the run as stopping so the SDK's receive loop exits
            # cleanly on its own close() instead of treating ours as a
            # connection error.
            self._stream.stop()
        except Exception:
            logger.exception("error stopping websocket stream")
        self._health.mark_stopped()
