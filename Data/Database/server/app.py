"""Network interface for market-data-db.

Thin HTTP wrapper around the unmodified `MarketDataDB` library so that other
AlgoT containers (ingestion, backtesting, feature engineering, ...) can write
and query the data lake over the dedicated `algot-db-net` Docker network
instead of importing the library and touching the data files directly.

All business logic (schema, dedup, compaction, metadata, backup) lives in
`market_data_db/*` and is untouched; this module only does transport,
authentication and (de)serialization.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from market_data_db import DBConfig, MarketDataDB
from market_data_db.schema import Dataset

from .auth import require_api_key

# --------------------------------------------------------------------------- logging --

class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json

        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload)


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


_configure_logging()
log = logging.getLogger("market_data_db.server")

# ---------------------------------------------------------------------------- db init --

_db: MarketDataDB | None = None


def get_db() -> MarketDataDB:
    assert _db is not None, "database not initialized"
    return _db


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db
    if not os.environ.get("MARKET_DATA_DB_API_KEYS"):
        log.error("MARKET_DATA_DB_API_KEYS is not set; refusing to start")
        raise RuntimeError("MARKET_DATA_DB_API_KEYS must be set to at least one secret key")

    root = os.environ.get("MARKET_DATA_DB_ROOT", "/data")
    metadata_url = os.environ.get("MARKET_DATA_DB_METADATA_URL") or None
    config = DBConfig(root=root, metadata_url=metadata_url)
    log.info("starting market-data-db server", extra={"extra_fields": {"root": str(config.root)}})
    _db = MarketDataDB(config)
    try:
        yield
    finally:
        log.info("shutting down: flushing and closing database")
        if _db is not None:
            _db.close()
        _db = None


def _docs_enabled() -> bool:
    """Whether to expose /docs, /redoc and /openapi.json.

    These are unauthenticated by design in FastAPI: gating them behind the API-key
    dependency is not possible without reimplementing the routes, so they are simply
    off unless explicitly enabled. In the cluster nothing should be publishing the
    full API surface -- including the write and backup/restore routes -- to any
    client that can reach port 8000. Set MARKET_DATA_DB_ENABLE_DOCS=1 for local
    development.
    """
    return os.environ.get("MARKET_DATA_DB_ENABLE_DOCS", "").strip().lower() in {"1", "true", "yes"}


_docs = _docs_enabled()

app = FastAPI(
    title="market-data-db",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)


@app.middleware("http")
async def _access_log(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error", extra={"extra_fields": {"path": request.url.path}})
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    log.info(
        "request",
        extra={
            "extra_fields": {
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(duration_ms, 2),
            }
        },
    )
    return response


# -------------------------------------------------------------------------------- health --

@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness/readiness probe. No auth required (used by Docker healthcheck)."""
    try:
        db = get_db()
        with db._Session() as session:  # noqa: SLF001 - cheap connectivity check
            session.execute(__import__("sqlalchemy").text("SELECT 1"))
        return {"status": "ok", "root": str(db.config.root)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"not ready: {exc}") from exc


@app.get("/v1/storage/status")
def storage_status(db: MarketDataDB = Depends(get_db), _: str = Depends(require_api_key)) -> dict[str, Any]:
    """Expose capacity to the backfill worker without exposing file contents."""
    capacity = shutil.disk_usage(db.config.root)
    minimum_free = max(5 * 1024**3, capacity.total // 10)
    return {
        "total_bytes": capacity.total,
        "free_bytes": capacity.free,
        "minimum_free_bytes": minimum_free,
        "backfill_allowed": capacity.free > minimum_free,
    }


# --------------------------------------------------------------------------------- models --

class WriteRequest(BaseModel):
    provider: str
    source: str
    records: list[dict[str, Any]]


class ReadRequest(BaseModel):
    symbols: list[str] | None = None
    providers: list[str] | None = None
    sources: list[str] | None = None
    start: datetime | None = None
    end: datetime | None = None
    as_of: datetime | None = None
    dedup: str | None = "canonical"
    columns: list[str] | None = None
    limit: int | None = None


class RestoreRequest(BaseModel):
    archive: str = Field(..., description="Backup filename under <root>/backups, e.g. backup-20260101T000000Z.tar.gz")
    overwrite: bool = False


def _df_to_json(df) -> list[dict[str, Any]]:
    import json

    return json.loads(df.to_json(orient="records", date_format="iso", default_handler=str))


def _df_records_json(df) -> str:
    """Serialise a frame to a JSON array using pandas' own (C) writer."""
    return df.to_json(orient="records", date_format="iso", default_handler=str)


def _rows_response(df) -> Response:
    """Return {"rows": [...]} without round-tripping the rows through Python.

    The obvious `return {"rows": _df_to_json(df)}` costs three conversions for
    one response: pandas writes JSON, json.loads rebuilds every row as a dict,
    and then FastAPI serialises those dicts back into JSON. The middle two are
    pure overhead, and on a large read they dominate both the response time and
    the server's peak memory -- the row dicts are far bigger in memory than the
    bytes they came from.

    Emitting pandas' output directly keeps the wire format byte-for-byte
    identical (date_format and default_handler are unchanged) while doing the
    work once.
    """
    return Response(
        content='{"rows":' + _df_records_json(df) + "}",
        media_type="application/json",
    )


def _dataset(name: str) -> Dataset:
    try:
        return Dataset(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"unknown dataset '{name}'") from exc


# ---------------------------------------------------------------------------- write/read --

@app.post("/v1/datasets/{dataset}/write")
def write(dataset: str, body: WriteRequest, db: MarketDataDB = Depends(get_db), _: str = Depends(require_api_key)):
    ds = _dataset(dataset)
    try:
        rows_written = db.write(ds, body.records, provider=body.provider, source=body.source)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"rows_written": rows_written}


@app.post("/v1/datasets/{dataset}/read")
def read(dataset: str, body: ReadRequest, db: MarketDataDB = Depends(get_db), _: str = Depends(require_api_key)):
    ds = _dataset(dataset)
    df = db.read(
        ds,
        symbols=body.symbols,
        providers=body.providers,
        sources=body.sources,
        start=body.start,
        end=body.end,
        as_of=body.as_of,
        dedup=body.dedup,
        columns=body.columns,
        limit=body.limit,
    )
    return _rows_response(df)


@app.get("/v1/datasets/{dataset}/latest/{symbol}")
def latest(dataset: str, symbol: str, db: MarketDataDB = Depends(get_db), _: str = Depends(require_api_key)):
    ds = _dataset(dataset)
    row = db.get_latest(ds, symbol)
    if row is None:
        return {"row": None}
    return {"row": _df_to_json(row.to_frame().T)[0]}


@app.post("/v1/datasets/{dataset}/point-in-time")
def point_in_time(dataset: str, body: ReadRequest, db: MarketDataDB = Depends(get_db), _: str = Depends(require_api_key)):
    ds = _dataset(dataset)
    if body.as_of is None:
        raise HTTPException(status_code=400, detail="as_of is required")
    df = db.point_in_time(ds, body.as_of, symbols=body.symbols, providers=body.providers, dedup=body.dedup or "canonical")
    return _rows_response(df)


# -------------------------------------------------------------------------- maintenance --

@app.post("/v1/datasets/{dataset}/compact")
def compact(
    dataset: str,
    provider: str | None = None,
    source: str | None = None,
    symbol: str | None = None,
    db: MarketDataDB = Depends(get_db),
    _: str = Depends(require_api_key),
):
    ds = _dataset(dataset)
    rows = db.compact(ds, provider=provider, source=source, symbol=symbol)
    return {"rows_compacted": rows}


@app.get("/v1/datasets/{dataset}/coverage")
def coverage(dataset: str, symbol: str | None = None, db: MarketDataDB = Depends(get_db), _: str = Depends(require_api_key)):
    ds = _dataset(dataset)
    df = db.coverage(ds, symbol=symbol)
    return _rows_response(df)


@app.get("/v1/datasets/{dataset}/row-count")
def row_count(dataset: str, dedup: str = "exact", db: MarketDataDB = Depends(get_db), _: str = Depends(require_api_key)):
    """dedup: 'exact' (default, per-provider dedup), 'canonical' (collapse
    live-over-backfill too), or 'none' (every physical row, no dedup)."""
    ds = _dataset(dataset)
    mode = None if dedup.lower() == "none" else dedup
    return {"row_count": db.row_count(ds, dedup=mode)}


@app.get("/v1/datasets/{dataset}/integrity/duplicates")
def duplicates(dataset: str, db: MarketDataDB = Depends(get_db), _: str = Depends(require_api_key)):
    ds = _dataset(dataset)
    report = db.check_integrity(ds)
    return {
        "duplicate_keys": report.duplicate_keys,
        "duplicate_rows": report.duplicate_rows,
        "sample": _df_to_json(report.sample),
    }


@app.get("/v1/datasets/{dataset}/integrity/gaps")
def gaps(
    dataset: str,
    symbol: str,
    max_gap_seconds: float,
    db: MarketDataDB = Depends(get_db),
    _: str = Depends(require_api_key),
):
    ds = _dataset(dataset)
    report = db.find_gaps(ds, symbol, max_gap_seconds=max_gap_seconds)
    return {"symbol": report.symbol, "gaps": _df_to_json(report.gaps)}


# ------------------------------------------------------------------------------- backups --

@app.post("/v1/backups")
def create_backup(label: str | None = None, db: MarketDataDB = Depends(get_db), _: str = Depends(require_api_key)):
    path = db.backup(label=label)
    return {"archive": path.name, "path": str(path)}


@app.get("/v1/backups")
def list_backups(db: MarketDataDB = Depends(get_db), _: str = Depends(require_api_key)):
    backups = sorted(db.config.backup_root.glob("*.tar.gz"))
    return {"backups": [{"name": p.name, "bytes": p.stat().st_size} for p in backups]}


@app.post("/v1/backups/restore")
def restore(body: RestoreRequest, db: MarketDataDB = Depends(get_db), _: str = Depends(require_api_key)):
    archive_path = db.config.backup_root / body.archive
    if not archive_path.exists() or archive_path.parent != db.config.backup_root:
        raise HTTPException(status_code=404, detail="backup archive not found")
    try:
        db.restore(archive_path, overwrite=body.overwrite)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"restored": body.archive}
