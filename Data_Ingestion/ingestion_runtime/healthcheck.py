"""Docker HEALTHCHECK probe for the live-ingestion process.

Checks that live_runner.py's heartbeat file (touched once per flush-loop
tick -- see LIVE_DB_FLUSH_INTERVAL_SECONDS) was updated recently. A stale or
missing heartbeat means the process is hung or has crashed without the
container exiting (shouldn't happen given the signal handling in
live_runner.py, but this is the backstop).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HEARTBEAT_PATH = Path(os.environ.get("LIVE_HEARTBEAT_PATH", "/tmp/live_ingestion_heartbeat"))
MAX_STALENESS_SECONDS = float(os.environ.get("LIVE_HEARTBEAT_MAX_STALENESS_SECONDS", "60"))


def main() -> int:
    if not HEARTBEAT_PATH.exists():
        print(f"heartbeat file {HEARTBEAT_PATH} does not exist yet")
        return 1
    try:
        heartbeat_ts = float(HEARTBEAT_PATH.read_text().strip())
    except (ValueError, OSError) as exc:
        print(f"could not read heartbeat file: {exc}")
        return 1
    age = time.time() - heartbeat_ts
    if age > MAX_STALENESS_SECONDS:
        print(f"heartbeat is stale: {age:.1f}s old (max {MAX_STALENESS_SECONDS}s)")
        return 1
    print(f"ok: heartbeat {age:.1f}s old")
    return 0


if __name__ == "__main__":
    sys.exit(main())
