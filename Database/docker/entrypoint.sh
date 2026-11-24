#!/bin/sh
set -eu

: "${MARKET_DATA_DB_ROOT:=/data}"
: "${HOST:=0.0.0.0}"
: "${PORT:=8000}"
: "${UVICORN_WORKERS:=1}"

if [ -z "${MARKET_DATA_DB_API_KEYS:-}" ]; then
    echo "FATAL: MARKET_DATA_DB_API_KEYS must be set (comma-separated secret keys)" >&2
    exit 1
fi

mkdir -p "$MARKET_DATA_DB_ROOT"

# exec replaces this shell as PID 1 so SIGTERM/SIGINT reach uvicorn directly,
# which triggers the FastAPI lifespan shutdown hook (flush + close the db)
# instead of being swallowed by an intermediate shell process.
exec uvicorn server.app:app \
    --host "$HOST" \
    --port "$PORT" \
    --workers "$UVICORN_WORKERS" \
    --no-access-log \
    --timeout-graceful-shutdown 30
