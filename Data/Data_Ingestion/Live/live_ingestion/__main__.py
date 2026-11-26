"""Standalone entrypoint: python -m live_ingestion

Runs the engine and logs every normalized event plus periodic health
summaries until interrupted (Ctrl+C).
"""
from __future__ import annotations

import asyncio
import logging
import signal

from .config import ConfigError, Settings
from .engine import MarketDataEngine
from .logging_setup import configure_logging

logger = logging.getLogger("live_ingestion.main")


async def _log_events(engine: MarketDataEngine) -> None:
    async for event in engine.events():
        logger.info("%s %s %s", event.type.value, event.symbol, event.payload)


async def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        raise SystemExit(1)

    configure_logging(settings.log_level)
    engine = MarketDataEngine(settings)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await engine.start()
    consumer = asyncio.create_task(_log_events(engine))
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        consumer.cancel()
        await engine.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
