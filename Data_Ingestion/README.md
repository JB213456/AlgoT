# AlgoT Data Ingestion Platform

A single, independently deployable Docker image containing:

- **Live ingestion** (`live_ingestion`, under `Live/`) — continuous Alpaca
  free-IEX websocket + REST ingestion. Runs as the container's main (PID 1)
  process.
- **Historical backfill** (`backfill_engine`, under `Backfill/`) — the
  `market-backfill` CLI (Alpaca + London Strategic Edge free tiers),
  invoked on demand via `docker compose exec`.
- **Deployment glue** (`ingestion_runtime/`) — wires `live_ingestion`'s
  existing public interface (`subscribe()`/`start()`/`stop()`) to an
  optional HTTP sink, plus the container entrypoint and healthcheck.

It deliberately does **not** contain the `market_data_db` package, its
server, or its Dockerfile — that's a separately built and deployed image
(see `../Database`). This image talks to it only as an HTTP client over the
network, so the two stay independently buildable/deployable and this image
carries none of that service's heavier dependencies (duckdb/pyarrow/
sqlalchemy).

Nothing in `Live/` or `Backfill/` was rewritten to build this: both engines
are installed as ordinary Python packages and used through their existing
public APIs (`MarketDataEngine.subscribe()`, `BackfillEngine`/`market-backfill`
CLI). The only additions are the small `ingestion_runtime/` glue layer and an
optional HTTP sink inside `backfill_engine` (`market_db_client.py`, a soft
dependency that no-ops when `MARKET_DATA_DB_URL` is unset).

## Quick start

```bash
cd /home/james/AlgoT/Data_Ingestion
cp .env.example .env
# edit .env: ALPACA_API_KEY / ALPACA_SECRET_KEY (required), LSE_API_KEY
# (optional, needed only for London Strategic Edge backfills)

docker compose up -d
```

That's it — live ingestion starts immediately and keeps running. Check it:

```bash
docker compose ps                        # should show "healthy"
docker compose logs -f data-ingestion    # live engine + health lines
```

## Historical backfill (while live ingestion keeps running)

```bash
docker compose exec data-ingestion market-backfill --symbol AAPL
docker compose exec data-ingestion market-backfill --symbol SPY --provider alpaca
docker compose exec data-ingestion market-backfill --symbol "BTC/USD"
docker compose exec data-ingestion market-backfill --symbol AAPL --coverage
```

Full flag reference: `Backfill/README.md`. The backfill engine's own
coverage-tracking SQLite lives at `/data/backfill_coverage/market_data.sqlite3`
on the `ingestion_data` volume, so repeated runs only fetch what's missing
and survive container restarts.

## Connecting to market-data-db

Leave `MARKET_DATA_DB_URL` unset in `.env` to run ingestion with no shared-DB
writes (useful before that service exists, or for standalone testing). Once
it's up (see `../Database`), to wire this container to it:

```bash
# 1. Join the network market-data-db publishes itself on
docker network connect algot-db-net algot-data-ingestion

# 2. Set these in .env, then recreate this container
MARKET_DATA_DB_URL=http://market-data-db:8000
MARKET_DATA_DB_API_KEY=<one of that service's MARKET_DATA_DB_API_KEYS>

docker compose up -d --force-recreate
```

From then on:
- Every live trade/quote/bar event is batched and POSTed to market-data-db
  (`ingestion_runtime/live_db_client.py`), flushed every
  `LIVE_DB_FLUSH_INTERVAL_SECONDS` (default 5s) and on shutdown.
- Every record `market-backfill` fetches is also POSTed there
  (`backfill_engine/market_db_client.py`), in addition to this engine's own
  coverage SQLite.

Both clients duplicate the small, pure record-conversion logic already
defined in `market_data_db/adapters/{live,backfill}_adapter.py` (that
package is the source of truth for the wire schema) rather than importing
it, specifically so this image has zero dependency on that package.

## Operational properties

| Requirement | How it's met |
|---|---|
| Continuous live ingestion | `ingestion_runtime.live_runner` is the container's PID 1; runs until signaled |
| CLI backfill access | `market-backfill` console script, installed in the image, run via `docker compose exec` |
| Persistent storage | Named volume `ingestion_data` → `/data` (backfill coverage SQLite); survives restarts (verified) |
| Secure credentials | `.env` (gitignored), injected only at container runtime via `env_file`/`environment` — never baked into image layers (verified: `docker run` with no env vars fails config validation; `docker history` has no secrets) |
| Configurable instruments/providers | `ALPACA_SYMBOLS`, `ALPACA_SUBSCRIPTIONS`, `ALPACA_DATA_FEED`, `LSE_*`, all via `.env` |
| Auto restart / failure recovery | `restart: unless-stopped` in `docker-compose.yml` (verified configured via `docker inspect`; this sandbox's nested Docker daemon doesn't actually execute restart policies even for a bare test container, so live auto-restart-after-crash could not be observed here — verify on a normal Docker host) |
| Health checks | `HEALTHCHECK` → `ingestion_runtime.healthcheck` checks a heartbeat file touched every flush tick (verified: `docker compose ps` shows `healthy`) |
| Structured logging | Existing `live_ingestion`/`backfill_engine` leveled, named loggers; `docker compose logs` |
| CPU/memory limits | `deploy.resources.limits` + `mem_limit`/`cpus` in compose (verified via `docker inspect`: 2 CPUs, 1 GiB) |
| Graceful shutdown | SIGTERM/SIGINT handled in `live_runner.py`: stops the engine, flushes the HTTP sink, exits cleanly (verified in logs) |
| DB integrity on shutdown | Backfill's SQLite coverage store commits per batch (pre-existing `backfill_engine` behavior); confirmed data survives a container restart |
| Internal data-access interface | Provided by the separate `market-data-db` HTTP service (see `../Database`); this image is one of its clients |

## What was verified end-to-end in this environment

- `docker compose up -d` → container starts and reaches `healthy`.
- Live engine connects to Alpaca's real IEX websocket and REST snapshot
  endpoint (`alpaca.data.live.websocket: connected`, REST poller message
  count increasing) using real credentials.
- `docker compose exec data-ingestion market-backfill --symbol AAPL` (and
  `SPY`) ran concurrently with live ingestion and returned real historical
  bars from Alpaca.
- The backfill coverage SQLite persisted real data across
  `docker compose restart` and a full container recreate.
- Graceful shutdown logs (`stopping engine and flushing remaining events` →
  `engine stopped` → `shutdown complete`) on every restart.
- No secrets present in the built image (`docker history`, and the image
  refuses to start without env-supplied credentials).
- CPU/memory limits applied (`docker inspect`: `NanoCpus`, `Memory`).
- 52 unit tests pass (`Backfill/`: 46, `ingestion_runtime/`: 6), all against
  mocked HTTP — no network access required to run them.

**Not verified in this sandbox**: Docker's `restart: unless-stopped` policy
actually restarting the container after a crash/SIGKILL. The policy is
correctly configured (confirmed via `docker inspect`), but this sandbox's
nested Docker daemon didn't execute the restart-policy supervisor even for
an unrelated bare test container (`docker run --restart unless-stopped ...`
also stayed exited after `docker kill`) -- an environment limitation, not a
configuration issue. Re-verify on a normal Docker host:
`docker kill -s SIGKILL algot-data-ingestion && sleep 5 && docker compose ps`
should show it back up.

## Project layout

```
Data_Ingestion/
  Dockerfile                 Multi-stage build: Live + Backfill + ingestion_runtime
  docker-compose.yml         Single "data-ingestion" service
  .env.example / .env        Credentials and tunables (gitignored)
  Live/                      live_ingestion package (unchanged) + new pyproject.toml
  Backfill/                  backfill_engine package + new market_db_client.py (optional HTTP sink)
  ingestion_runtime/         Container glue: live_runner.py, live_db_client.py, healthcheck.py
```

## Local development (outside Docker)

Each sub-project still works exactly as before:

```bash
cd Live && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cd Backfill && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && pip install -e .
```

See `Live/README.md` and `Backfill/README.md` for details.
