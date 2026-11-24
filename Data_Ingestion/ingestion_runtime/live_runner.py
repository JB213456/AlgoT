"""Container entrypoint process: runs live_ingestion.MarketDataEngine
continuously and (optionally) streams every normalized event to the
market-data-db service over HTTP.

This is deployment glue, not a change to live_ingestion itself -- it only
uses the engine's existing public interface (`subscribe()`, `start()`,
`stop()`, `health_snapshot()`).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from pathlib import Path

from live_ingestion.config import ConfigError, Settings
from live_ingestion.engine import MarketDataEngine
from live_ingestion.logging_setup import configure_logging

from .live_db_client import LiveHttpSink

logger = logging.getLogger("live_ingestion.platform.runner")

HEARTBEAT_PATH = Path(os.environ.get("LIVE_HEARTBEAT_PATH", "/tmp/live_ingestion_heartbeat"))
DB_FLUSH_INTERVAL_SECONDS = float(os.environ.get("LIVE_DB_FLUSH_INTERVAL_SECONDS", "5"))


def _build_sink() -> LiveHttpSink | None:
    base_url = os.environ.get("MARKET_DATA_DB_URL", "").strip()
    api_key = os.environ.get("MARKET_DATA_DB_API_KEY", "").strip()
    if not base_url:
        logger.warning("MARKET_DATA_DB_URL not set -- running without persisting events to market-data-db")
        return None
    if not api_key:
        logger.warning("MARKET_DATA_DB_URL is set but MARKET_DATA_DB_API_KEY is not -- "
                        "skipping market-data-db integration")
        return None
    return LiveHttpSink(base_url, api_key, provider="alpaca")


async def _heartbeat_and_flush_loop(engine: MarketDataEngine, sink: LiveHttpSink | None, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            if sink is not None:
                flushed = await sink.flush()
                if flushed:
                    logger.debug("flushed to market-data-db: %s", flushed)
            HEARTBEAT_PATH.write_text(str(time.time()))
        except Exception:  # noqa: BLE001 - never let the heartbeat/flush loop die the process
            logger.exception("heartbeat/flush tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=DB_FLUSH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        raise SystemExit(1)

    configure_logging(settings.log_level)
    logger.info("starting live ingestion platform runner")

    engine = MarketDataEngine(settings)
    sink = _build_sink()
    if sink is not None:
        engine.subscribe(sink.handle_event)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await engine.start()
    flush_task = asyncio.create_task(_heartbeat_and_flush_loop(engine, sink, stop))

    try:
        await stop.wait()
    finally:
        logger.info("shutting down: stopping engine and flushing remaining events")
        flush_task.cancel()
        try:
            await flush_task
        except asyncio.CancelledError:
            pass
        await engine.stop()
        if sink is not None:
            await sink.flush()
            await sink.close()
        logger.info("shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
