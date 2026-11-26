# Live Market Data Ingestion Engine

Standalone, live-only market data ingestion engine built on Alpaca's **free
IEX** market data tier. It ingests trades, quotes and OHLCV bars over
websocket, cross-checks with periodic REST snapshot polling, validates and
deduplicates everything, and exposes a single async interface for downstream
consumers. No historical backfill, strategies, feature engineering, or order
execution — ingestion only.

## What it does

- **Websocket ingestion** via alpaca-py's `StockDataStream` for trades,
  quotes, minute bars, updated bars, daily bars and trading-status messages
  (configurable subset).
- **REST polling** of `/stocks/snapshots` as a supplementary cross-check /
  gap-filler, rate-limited to the free-tier REST quota via a token-bucket
  limiter.
- **Reconnection & subscription recovery**: alpaca-py's `StockDataStream`
  already reconnects with backoff and re-subscribes automatically; the engine
  adds an outer supervisor that restarts the whole websocket client if the
  SDK's own loop ever exits (e.g. a fatal auth error).
- **Validation, dedup, normalization**: every raw record is validated
  (positive prices, consistent OHLC, non-negative sizes/volume), timestamps
  are normalized to timezone-aware UTC, and a bounded-memory per-symbol dedup
  layer drops redelivered/overlapping records from the websocket and REST
  paths.
- **Unified downstream interface** (`MarketDataEngine`):
  - `async for event in engine.events(): ...` — normalized event stream
  - `engine.subscribe(callback)` — register a sync/async callback for every event
  - `engine.latest(symbol)` — latest cached trade/quote/bar per symbol
  - `engine.health_snapshot()` — connection health, message counts, dropped/invalid counts
- **Health monitoring & logging**: per-connection state (connecting/connected/
  disconnected/error), message counts, reconnect counts, staleness detection,
  logged on an interval.

## Project layout

```
live_ingestion/
  config.py        Settings loaded from environment / .env
  models.py        Trade, Quote, Bar, MarketDataEvent
  normalize.py      Validation + UTC timestamp normalization
  dedup.py          Bounded-memory deduplication
  rate_limiter.py   Async token-bucket limiter for REST calls
  health.py         Connection health tracking + periodic logging
  ws_client.py      Wraps alpaca-py StockDataStream
  rest_poller.py    Periodic REST snapshot polling
  engine.py         MarketDataEngine — unified orchestrator
  __main__.py       `python -m live_ingestion` standalone runner
  tests/            pytest unit tests (mocked data, no network)
scripts/
  live_smoke_test.py  Live verification against the real Alpaca API
```

## Setup

```bash
cd /home/james/AlgoT/Data_Ingestion/Live
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: set ALPACA_API_KEY / ALPACA_SECRET_KEY, and optionally
# ALPACA_SYMBOLS / ALPACA_SUBSCRIPTIONS / poll interval / rate limit.
```

`.env` is gitignored — never commit real API keys.

## Run it

```bash
source .venv/bin/activate
python -m live_ingestion
```

Logs every normalized event and periodic health summaries; Ctrl+C to stop
(handled gracefully — websocket is stopped cleanly, tasks cancelled).

## Use it from another process (unified interface)

```python
import asyncio
from live_ingestion import MarketDataEngine, Settings

async def main():
    engine = MarketDataEngine(Settings.from_env())
    await engine.start()
    async for event in engine.events():
        print(event.type, event.symbol, event.payload)

asyncio.run(main())
```

## Tests

```bash
source .venv/bin/activate
python -m pytest -q
```

34 unit tests cover config validation, normalization/validation rules,
deduplication, the rate limiter, health-state transitions, and the engine's
event pipeline (trade/quote/bar handling, dedup, invalid-record rejection,
callback fan-out, queue overflow handling). These run fully offline against
mocked SDK objects — no network or API keys required.

## Live verification against Alpaca's real API

```bash
source .venv/bin/activate
python scripts/live_smoke_test.py
```

This starts the full engine against your real `.env` credentials, runs for
~25 seconds, and prints an event-count and health summary, exiting non-zero
if the websocket never reaches a connected state or nothing valid was
received. Verified live run output (market closed at test time, so only
REST-snapshot and no live websocket ticks — expected outside trading hours):

```
Event counts by type: {'trade': 3, 'quote': 3, 'bar': 3}
websocket: state=connected, reconnects=0, errors=0
rest_poller: state=connected, messages_received=3, errors=0
PASS: engine connected to Alpaca's live API and processed data.
```

During regular US market hours (9:30am–4:00pm ET, Mon–Fri) you should also
see `websocket` `messages_received` climb as live trades/quotes/bars stream
in for liquid symbols like AAPL/MSFT/TSLA.

## Configuration reference (`.env`)

| Variable | Purpose |
|---|---|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Alpaca credentials |
| `ALPACA_DATA_FEED` | Must be `iex` (free tier) |
| `ALPACA_SYMBOLS` | Comma-separated instruments, e.g. `AAPL,MSFT,TSLA` |
| `ALPACA_SUBSCRIPTIONS` | Subset of `trades,quotes,bars,updated_bars,daily_bars,statuses` |
| `ALPACA_WS_DATA_TIMEOUT` | Seconds of websocket silence before forcing a reconnect (blank = disabled) |
| `ALPACA_REST_POLL_INTERVAL_SECONDS` | REST snapshot poll interval; `0` disables REST polling |
| `ALPACA_REST_RATE_LIMIT_PER_MINUTE` | REST token-bucket limit (free tier: 200/min) |
| `ALPACA_HEALTH_LOG_INTERVAL_SECONDS` | How often health is logged |
| `ALPACA_QUEUE_MAX_SIZE` | Max buffered events before oldest are dropped |
| `LOG_LEVEL` | Python logging level |

## Known limitations (by design, per scope)

- IEX free feed only (no SIP/OTC — those need a paid subscription; the
  config explicitly rejects non-`iex` feeds).
- No historical data collection, strategies, feature engineering, or order
  execution — ingestion only, as scoped.
