# Historical Market Data Backfill Engine

Standalone CLI (`market-backfill`) that backfills historical market data for
a symbol from two free-tier providers:

- **Alpaca** (free IEX feed) — equities & crypto OHLCV bars, options bars.
- **London Strategic Edge** ("LSE Data 3.0") — OHLCV bars, live option chain
  with greeks/IV, option price bars, company-profile fundamentals, and
  corporate actions (splits/dividends) — on whichever markets your free key
  is entitled to.

No live streaming, strategies, feature engineering, backtesting, or order
execution here — backfill only. See the sibling `../Live` engine for
real-time ingestion.

Both providers are hit as plain HTTPS/REST calls written against their
documented APIs — this project does **not** install either vendor's Python
SDK, so no third-party code runs beyond `httpx`/`python-dotenv`.

## A note on London Strategic Edge

London Strategic Edge is a young company (~7 months old at the time of
writing) with no independently verifiable business registration found during
review — but its documented API works and returns real market data (verified
live against AAPL during development: OHLCV bars, a 3,362-contract option
chain with live greeks/IV, option price bars, and a company-profile
fundamentals record). Nothing found indicates it's malicious; nothing found
proves a long track record either. Treat the data as free-tier/best-effort,
not an institutional-grade feed, and keep your `LSE_API_KEY` private.

## Setup

```bash
cd /home/james/AlgoT/Data_Ingestion/Backfill
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .          # installs the `market-backfill` CLI entry point

cp .env.example .env
# edit .env: ALPACA_API_KEY / ALPACA_SECRET_KEY and/or LSE_API_KEY
```

`.env` is gitignored. By default the SQLite database lives at
`backfill_data/market_data.sqlite3` inside this project; set
`BACKFILL_DB_PATH` (e.g. to a path under `AlgoT/Database/`) to point it
somewhere else once that store exists.

## CLI usage

```bash
# All datasets, all resolutions, full available history, both providers:
market-backfill --symbol AAPL

# A specific provider:
market-backfill --symbol SPY --provider alpaca

# Crypto (quote it, mapped automatically — Alpaca "BTC/USD", LSE "BTCUSDT"):
market-backfill --symbol BTC/USD

# A bounded date range:
market-backfill --symbol AAPL --start 2020-01-01 --end 2026-11-24

# Specific datasets / resolutions:
market-backfill --symbol AAPL --dataset bars,option_chain --resolution 1Day

# Just show what's already backfilled for a symbol:
market-backfill --symbol AAPL --coverage
```

### Flags

| Flag | Meaning |
|---|---|
| `--symbol` (required) | Canonical symbol, e.g. `AAPL`, `SPY`, `"BTC/USD"` |
| `--start` / `--end` | `YYYY-MM-DD`. Omit either for "all available" (start defaults to 2000-01-01, end to today) |
| `--provider` | `alpaca`, `lse`, or `all` (default) |
| `--dataset` | Comma list of `bars,option_chain,option_bars,fundamentals,corporate_actions`, or `all` (default) |
| `--resolution` | Comma list of resolutions (e.g. `1Day`, `1Min` for Alpaca; `1d`, `1h` for LSE). Default: each provider's documented free-tier resolution set |
| `--db-path` | Override `BACKFILL_DB_PATH` for this run |
| `--coverage` | Print coverage for `--symbol` and exit (no network calls) |

Running the same command again only fetches what's missing: coverage is
tracked per `(provider, feed, dataset, symbol, resolution)` as a
`[min_ts, max_ts]` window, and only the leading/trailing gaps against your
requested range are re-fetched. Storage is idempotent either way (primary
key on provider+feed+symbol+resolution+timestamp), so even a slightly
overlapping re-fetch at a chunk boundary can't duplicate rows.

## What's covered, and where it comes from

| Dataset | Alpaca (free/IEX) | LSE |
|---|---|---|
| OHLCV bars (stocks, crypto) | Yes — `1Min/5Min/15Min/1Hour/1Day` | Yes — `1m/5m/15m/1h/1d` (plan/market dependent) |
| Option price bars | Yes (since Feb 2024) | Yes |
| Option chain + IV + greeks | Not implemented (Basic plan has no historical chain) | Yes — `/v3/options/chain` |
| Trades / quotes (tick) | Not implemented | Free markets mostly return `data-unavailable` (paid feeds) |
| Fundamentals / company profile | Not available on Alpaca's market data API | Yes — `/v3/reference/export` |
| Corporate actions (splits/dividends) | Not available on Alpaca's market data API | Yes — `/v3/factors` |

`option_bars` discovers contracts via LSE's live chain, then backfills bars
for a bounded, near-term/low-strike subset (`max_contracts`, default 20) —
full chains run into the thousands of contracts and would blow through
free-tier quotas for little benefit (most contracts never traded).

Any dataset/symbol/market combination your plan doesn't cover raises a
`DatasetUnavailable` internally; the engine logs it and keeps going with
everything else rather than aborting the run.

## Free-tier quota management

- **Alpaca**: 200 req/min (configurable), bounded request concurrency.
- **LSE**: 60 req/min, a rolling daily row quota, a rolling weekly byte
  quota, and a single concurrent export download (per the documented
  "one download open per key" limit) — all enforced client-side before
  a request goes out, in addition to honoring `Retry-After` on 429s.
- All HTTP calls retry on transient network errors and 429/5xx with
  exponential backoff (capped at `BACKFILL_MAX_RETRIES`), honoring a
  server-sent `Retry-After` when present.

## Project layout

```
backfill_engine/
  config.py          Settings from environment / .env
  models.py           Normalized dataclasses (Bar, Trade, Quote, OptionContract, ...)
  db.py                SQLite schema, upserts, coverage tracking
  symbol_mapping.py    Canonical <-> provider symbol mapping
  rate_limit.py        Token-bucket + rolling-quota limiters
  retry.py             Backoff/retry with Retry-After support
  validation.py        Bar sanity checks (OHLC consistency, non-negative volume)
  engine.py            Orchestrator: gap detection, chunking, concurrent download
  cli.py               `market-backfill` argument parsing and run loop
  providers/
    base.py            Provider interface + DatasetUnavailable
    alpaca.py           Alpaca REST client
    lse.py               London Strategic Edge REST client
  tests/               pytest unit tests (mocked HTTP via respx, no network)
```

## Tests

```bash
source .venv/bin/activate
python3 -m pytest -q
```

35 unit tests cover symbol mapping, the SQLite layer (upsert idempotency,
coverage widening, provider/feed provenance), rate limiting, gap/chunk
math, bar validation, and both providers' HTTP behavior (pagination,
CSV parsing, 4xx → `DatasetUnavailable`) against mocked responses — no
network access required to run them.

Live verification performed during development (both providers, real
credentials, real data returned): Alpaca stock bars (`AAPL`, daily),
Alpaca crypto bars (`BTC/USD`, daily), LSE stock bars (`AAPL`, daily), LSE
option chain (`AAPL`, 3,362 live contracts with greeks/IV), LSE option
price bars (resumable, gap-detected across two runs), and LSE fundamentals
(company profile).

## Known limitations

- Gap detection assumes historical data is immutable and contiguous once
  fetched; it only extends the leading/trailing edges of what's covered.
  It does not detect or backfill an interior gap you created by, say,
  deleting rows out of the middle of an already-covered range.
- `option_bars`' contract selection is a near-term/low-strike proxy, not a
  true at-the-money selection (no underlying spot price is modeled on
  `OptionContract` yet).
- Historical trades/quotes (tick data) are not implemented for either
  provider: Alpaca's endpoints weren't wired up, and LSE's free markets
  return `data-unavailable` for them (paid CME/Nasdaq feeds only).
