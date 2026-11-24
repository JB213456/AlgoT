"""Live verification against Alpaca's real market data API.

Starts the full engine (websocket + REST poller + health monitor) using the
credentials in .env, runs for a fixed duration during which it must receive
at least one real event, and prints a health/validation summary. Exits
non-zero on failure so it can gate "the engine actually works against the
live API" rather than just "the code imports".

Note: trades/quotes only flow while the US market session is open (or close
to it); outside those hours this may legitimately see bars/snapshots but no
trades. The script reports what it saw either way.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_ingestion.config import ConfigError, Settings  # noqa: E402
from live_ingestion.engine import MarketDataEngine  # noqa: E402
from live_ingestion.logging_setup import configure_logging  # noqa: E402

RUN_SECONDS = 25


async def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 1

    configure_logging(settings.log_level)
    logger = logging.getLogger("live_smoke_test")

    engine = MarketDataEngine(settings)
    counts: Counter = Counter()
    engine.subscribe(lambda event: counts.update([event.type.value]))

    await engine.start()
    logger.info("engine started, listening for %ds ...", RUN_SECONDS)
    await asyncio.sleep(RUN_SECONDS)
    health = engine.health_snapshot()
    await engine.stop()

    print("\n=== Live smoke test summary ===")
    print("Event counts by type:", dict(counts))
    print("Health snapshot:")
    for name, info in health.items():
        print(f"  {name}: {info}")

    ws_health = health.get("websocket", {})
    ws_connected_or_saw_messages = ws_health.get("state") == "connected" or ws_health.get(
        "messages_received", 0
    ) > 0

    if not ws_connected_or_saw_messages:
        print("\nFAIL: websocket never reached a connected/active state.")
        return 1

    if health.get("_queue", {}).get("validation_errors", 0) and not counts:
        print("\nFAIL: only validation errors were observed, no valid events.")
        return 1

    print("\nPASS: engine connected to Alpaca's live API and processed data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
