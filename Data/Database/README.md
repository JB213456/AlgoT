# market-data-db

Standalone market data storage layer for AlgoT. Consumes data from the
`live_ingestion` (Alpaca live) and `backfill_engine` (Alpaca + London Strategic
Edge historical) engines without requiring any changes to either, and serves it
back out through one unified Python interface.

Provider integrations, collectors, strategies, feature engineering, backtesting
and execution are explicitly out of scope — this is the database and storage
layer only.

## Architecture

- **Analytical store**: Apache Parquet files on local disk, queried with DuckDB.
  Two zones under `<root>/`:
  - `landing/` — small, immutable, append-only files written directly by
    ingestion (one file per `write()` call per `(symbol, date)` partition). No
    locking is needed here, so many concurrent writers (live + backfill, many
    symbols) never block each other.
  - `canonical/` — one compacted, deduplicated Parquet file per partition,
    produced by `compact()`. Queries transparently read both zones and merge
    them, so you never have to compact before reading — compaction only matters
    for query speed and storage efficiency.
  - `archive/` — landing files that have already been folded into canonical are
    moved here rather than deleted, so raw provider-specific observations are
    never destroyed.
  - Partitioning: `dataset=.../provider=.../source=.../symbol=.../date=...`,
    which is also how DuckDB prunes files for a query.
- **Operational metadata**: SQLite by default (one file under
  `<root>/metadata/metadata.sqlite`), or Postgres by passing a `postgresql://`
  URL — via SQLAlchemy, with a tiny versioned migration runner
  (`market_data_db/metadata/migrator.py`). Tracks every ingestion run
  (`ingestion_runs`) and running min/max/row-count coverage per
  `(dataset, provider, source, symbol)` (`coverage`), without needing to scan
  Parquet for integrity checks.
- **Idempotency / dedup**: every row carries `provider`, `source`
  (`"live"`/`"backfill"`), and `ingested_at`. Reads deduplicate by the dataset's
  primary key, keeping the most recently ingested row — so re-ingesting the same
  batch, or re-running a backfill job, is always safe. `compact()` makes this
  physical by rewriting the deduplicated rows into canonical files and moving the
  now-redundant landing files to `archive/`.
- **Live vs. backfill precedence**: `read(..., dedup="canonical")` (the default)
  additionally collapses across `provider`/`source` for the same symbol+time
  using each dataset's `source_priority` (live before backfill), so a historical
  backfill can never shadow a live observation. `dedup="exact"` instead preserves
  every provider's row untouched — use it when you need the raw, per-provider
  truth. `dedup=None` returns every physical row, for integrity checks.
- **Point-in-time reads**: `point_in_time(dataset, as_of, ...)` only considers
  rows that had already been ingested by `as_of`, and dedups among those — so you
  can reconstruct exactly what the database would have returned at any past
  moment, including before a late correction/restatement arrived.

See `market_data_db/schema.py` for the full dataset registry (bars/OHLCV,
trades, quotes, options quotes, options Greeks/IV, financial statements,
fundamentals, earnings, corporate actions, macro series, reference/symbol
metadata) — equities, crypto, forex, commodities and indices all share the same
`bars`/`trades`/`quotes` schemas via an `asset_class` column.

## Setup

```bash
cd Database
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Requires Python >= 3.10. Core dependencies: `duckdb`, `pyarrow`, `pandas`,
`sqlalchemy`, `filelock`. Install `.[postgres]` to use Postgres for metadata
instead of the SQLite default, or `.[dev]` for the test/benchmark suite.

## Usage

```python
from market_data_db import DBConfig, MarketDataDB
from market_data_db.schema import Dataset

db = MarketDataDB(DBConfig(root="./data"))  # or DBConfig() to use $MARKET_DATA_DB_ROOT

db.write(
    Dataset.BARS,
    [{
        "symbol": "AAPL", "asset_class": "equity", "timeframe": "1Min",
        "timestamp": "2024-01-02T14:30:00Z",
        "open": 190.1, "high": 190.4, "low": 189.9, "close": 190.2,
        "volume": 12000.0, "trade_count": 80, "vwap": 190.15,
    }],
    provider="alpaca", source="live",
)

latest = db.get_latest(Dataset.BARS, "AAPL")
history = db.read(Dataset.BARS, symbols=["AAPL"], start=..., end=...)
as_of_snapshot = db.point_in_time(Dataset.BARS, as_of=..., symbols=["AAPL"])

db.compact(Dataset.BARS)       # fold landing files into canonical, sweep-wide
db.backup(label="nightly")     # tar.gz snapshot under <root>/backups/
db.close()
```

### Wiring up the existing engines

**live_ingestion** (no code changes needed — it already exposes exactly this
hand-off surface in `MarketDataEngine`):

```python
from market_data_db.adapters.live_adapter import LiveSink

sink = LiveSink(db, provider="alpaca")
engine.subscribe(sink.handle_event)   # buffers in memory, non-blocking
# periodically (e.g. every few seconds) and on shutdown:
sink.flush()
```

**backfill_engine** (no code changes needed; translates its dataclasses —
`Bar`, `Trade`, `Quote`, `OptionContract`, `OptionBar`, `FundamentalRecord`,
`CorporateAction` — directly, independent of its own as-yet-unused SQLite
store):

```python
from market_data_db.adapters.backfill_adapter import ingest_record

for record in provider.fetch_bars(...):   # any backfill_engine model instance
    ingest_record(db, record)
```

## Integrity & operations

```python
db.check_integrity(Dataset.BARS)                      # duplicate-key report
db.find_gaps(Dataset.BARS, "AAPL", max_gap_seconds=90) # missing-bar detection
db.coverage(Dataset.BARS, symbol="AAPL")               # min/max/row_count
db.row_count(Dataset.BARS, dedup="exact")              # count without loading rows
```

## Testing & benchmarks

```bash
pip install -e .[dev]
pytest tests/ -q

python benchmarks/bench_ingest.py   # concurrent write throughput + compaction
python benchmarks/bench_query.py    # load 2M rows, then time latest/range/point-in-time reads
```

Representative numbers from this machine (8 workers, local SSD — see scripts
for exact parameters): ~80k rows/sec sustained concurrent ingest across 8
symbols/providers/sources at once, ~200k rows/sec compaction throughput,
~225k rows/sec bulk load, and sub-second single-symbol reads against a 2M-row
lake pre-compaction.

## Running as a standalone container

`market_data_db` is an embedded library (Parquet + DuckDB + SQLite/Postgres on
local disk) with no network server of its own. To run it as an independent,
network-reachable database — separate from the ingestion platform, with its
own auth, health checks, backups and resource limits — this repo adds a thin
FastAPI service (`server/app.py`) that wraps the *unmodified* `MarketDataDB`
class and exposes its operations over HTTP. All schema, dedup, compaction,
metadata and backup logic is untouched; the server is transport only.

### Deploy

```bash
cp .env.example .env
# edit .env: set MARKET_DATA_DB_API_KEYS to a long random secret
#   openssl rand -hex 32

docker compose build
docker compose up -d
docker compose ps                 # should report "healthy"
curl http://127.0.0.1:8000/health
```

Data lives in the named volume `algot_market_data_lake`, mounted at `/data`
inside the container (landing/canonical/archive zones + metadata DB +
backups), so it survives container restarts, rebuilds and `docker compose
down` (without `-v`).

### Other AlgoT containers: how to connect

The compose file publishes a dedicated bridge network, `algot-db-net`. Any
other AlgoT container (ingestion, backfill, feature engineering, backtesting)
should join that network and talk to the database by its service name,
`market-data-db`, on port 8000 — not via the host-published port, which exists
only for local debugging:

```yaml
# in the ingestion project's own docker-compose.yml
services:
  ingestion:
    # ...
    environment:
      MARKET_DATA_DB_URL: http://market-data-db:8000
      MARKET_DATA_DB_API_KEY: ${MARKET_DATA_DB_API_KEY}
    networks:
      - algot-db-net

networks:
  algot-db-net:
    external: true   # created by the database's own compose project
```

Every request must carry `X-API-Key: <key>`, matching one of the
comma-separated values in `MARKET_DATA_DB_API_KEYS`; requests without a valid
key get `401`. `/health` is the only unauthenticated route (used by the
Docker healthcheck).

#### HTTP interface

| Method & path | Purpose |
| --- | --- |
| `GET /health` | Liveness/readiness (no auth) |
| `POST /v1/datasets/{dataset}/write` | `{provider, source, records: [...]}` → `{rows_written}` |
| `POST /v1/datasets/{dataset}/read` | Filters (`symbols`, `providers`, `sources`, `start`, `end`, `as_of`, `dedup`, `columns`, `limit`) → `{rows: [...]}` |
| `GET /v1/datasets/{dataset}/latest/{symbol}` | Latest row for a symbol |
| `POST /v1/datasets/{dataset}/point-in-time` | Same filters as `read`, `as_of` required |
| `POST /v1/datasets/{dataset}/compact?provider=&source=&symbol=` | Fold landing → canonical |
| `GET /v1/datasets/{dataset}/coverage?symbol=` | Min/max/row-count per symbol |
| `GET /v1/datasets/{dataset}/row-count?dedup=exact\|canonical\|none` | Row count without loading rows |
| `GET /v1/datasets/{dataset}/integrity/duplicates` | Duplicate-key report |
| `GET /v1/datasets/{dataset}/integrity/gaps?symbol=&max_gap_seconds=` | Missing-observation report |
| `POST /v1/backups?label=` | Snapshot the lake + metadata DB to a `.tar.gz` under `/data/backups` |
| `GET /v1/backups` | List available backup archives |
| `POST /v1/backups/restore` | `{archive, overwrite}` — restore a snapshot (refuses to clobber existing data unless `overwrite: true`) |

`dataset` is one of the `Dataset` enum values (`bars`, `trades`, `quotes`,
`options_quotes`, `options_greeks`, `financial_statements`, `fundamentals`,
`earnings`, `corporate_actions`, `macro`, `reference`).

Example write/read from another container on `algot-db-net`:

```bash
curl -X POST http://market-data-db:8000/v1/datasets/bars/write \
  -H "X-API-Key: $MARKET_DATA_DB_API_KEY" -H "Content-Type: application/json" \
  -d '{"provider":"alpaca","source":"live","records":[{"symbol":"AAPL","asset_class":"equity",
       "timeframe":"1Min","timestamp":"2024-01-02T14:30:00Z","open":190.1,"high":190.4,
       "low":189.9,"close":190.2,"volume":12000.0,"trade_count":80,"vwap":190.15}]}'

curl -X POST http://market-data-db:8000/v1/datasets/bars/read \
  -H "X-API-Key: $MARKET_DATA_DB_API_KEY" -H "Content-Type: application/json" \
  -d '{"symbols":["AAPL"]}'
```

### Operations

**Logs** are structured JSON on stdout (one line per request plus lifecycle
events) — `docker compose logs -f market-data-db`.

**Health checks**: the container `HEALTHCHECK` and the compose `healthcheck`
both poll `GET /health`, which opens a real connection to the metadata DB, not
just a liveness ping.

**Restart/failure recovery**: `restart: unless-stopped` in `docker-compose.yml`
restarts the container on crash or daemon restart; the data volume is
unaffected, and `run_migrations()` re-runs (idempotently) on every startup, so
schema state is always consistent on boot. Verified by killing and restarting
the container with data present.

**Graceful shutdown**: the entrypoint `exec`s `uvicorn` as PID 1 (no wrapping
shell), so `SIGTERM` reaches it directly; its FastAPI `lifespan` shutdown hook
disposes the SQLAlchemy engine (closing open connections) before the process
exits. `stop_grace_period` is set to 35s to outlast in-flight requests.

**Resource limits**: `docker-compose.yml` sets CPU/memory limits
(`MARKET_DATA_DB_CPU_LIMIT`, `MARKET_DATA_DB_MEM_LIMIT` in `.env`, default
2 CPUs / 2GiB) via both `deploy.resources.limits` and the classic
`cpus`/`mem_limit` keys, so the limit applies whether or not swarm mode is in
use.

**Backups**:

```bash
# logical snapshot (tar.gz of the Parquet lake + metadata DB) via the API
curl -X POST http://127.0.0.1:8000/v1/backups -H "X-API-Key: $MARKET_DATA_DB_API_KEYS"
curl http://127.0.0.1:8000/v1/backups -H "X-API-Key: $MARKET_DATA_DB_API_KEYS"

# restore (stop writers first; restart the container afterwards so the
# metadata engine reopens against the restored SQLite file cleanly)
curl -X POST http://127.0.0.1:8000/v1/backups/restore \
  -H "X-API-Key: $MARKET_DATA_DB_API_KEYS" -H "Content-Type: application/json" \
  -d '{"archive":"backup-20260101T000000Z.tar.gz","overwrite":true}'
docker compose restart market-data-db

# full physical volume backup/restore (disaster recovery of the whole /data volume)
docker run --rm -v algot_market_data_lake:/data -v "$PWD/host-backups":/backup \
  debian:bookworm-slim tar czf /backup/volume-snapshot.tar.gz -C /data .
docker run --rm -v algot_market_data_lake:/data -v "$PWD/host-backups":/backup \
  debian:bookworm-slim sh -c "rm -rf /data/* && tar xzf /backup/volume-snapshot.tar.gz -C /data"
```

**Concurrency model**: writes are append-only and lock-free (safe for many
concurrent live + backfill writers at once); only `compact()` takes a
per-partition file lock. The server runs one process by default
(`UVICORN_WORKERS=1` in `.env`) with FastAPI dispatching each request to a
thread, matching the thread-based concurrency the library is designed and
tested for against its default SQLite metadata store. To scale to multiple
processes/replicas, set `MARKET_DATA_DB_METADATA_URL` to a `postgresql://` URL
(Postgres handles true concurrent connections) and raise `UVICORN_WORKERS`.

### Verified

Built, deployed and exercised end-to-end in this repo's dev environment:
health checks, API-key auth (valid/invalid/missing), writes, reads,
8-way concurrent writers (no lost rows, matches `tests/test_concurrency.py`'s
model), compaction, duplicate-report, backup, simulated data-loss + restore
with data verified byte-for-byte equal row counts, container restart with
data intact, graceful `SIGTERM` shutdown, and cross-container connectivity
over `algot-db-net` from a second container.
