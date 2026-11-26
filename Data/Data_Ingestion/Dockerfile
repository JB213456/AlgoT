# Single image for the AlgoT data-ingestion platform: continuous live
# ingestion (Alpaca) as the container's main process, plus the
# `market-backfill` CLI (Alpaca + London Strategic Edge) for on-demand
# historical backfills via `docker compose exec`.
#
# Deliberately does NOT contain the market_data_db package or server --
# that's a separately built/deployed image (see ../Database). This image
# talks to it only over HTTP (MARKET_DATA_DB_URL), so it stays independently
# deployable and has none of that service's heavier dependencies
# (duckdb/pyarrow/sqlalchemy).
FROM python:3.12-slim@sha256:a0e2dd78cc679ddfffea960fe45ecfcff9ac2f68d67917a0edfcde5c2100151b AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Security patches that the pinned base digest does not yet carry. The
# release gate in deployment/scripts/build-arm64-images.sh fails the build on
# any HIGH/CRITICAL CVE that has a fix available, so each pin below exists to
# clear a specific finding and must be bumped when a newer fix is published:
#
#   libzstd1  CVE-2026-124851  uninitialised-buffer information disclosure
#
# Applied in `base` so the builder and runtime stages share one patched layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libzstd1=1.5.7+dfsg-1+deb13u1 \
    && rm -rf /var/lib/apt/lists/*

# ---- builder: build wheels for all three local packages in one isolated layer ----
FROM base AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential=12.12 \
    && rm -rf /var/lib/apt/lists/*

COPY constraints.lock /build/constraints.lock
COPY Live/live_ingestion ./live_ingestion
COPY Live/pyproject.toml ./live_pyproject.toml
COPY Backfill/backfill_engine ./backfill_engine
COPY Backfill/pyproject.toml ./backfill_pyproject.toml
COPY ingestion_runtime ./ingestion_runtime
COPY pyproject.toml ./runtime_pyproject.toml

# Each sub-project keeps its own pyproject.toml at its own root for local
# dev (`pip install -e .` inside Live/ or Backfill/ unchanged); for the
# image build we stage each one under the name its own file expects.
RUN mkdir -p /build/live_src /build/backfill_src /build/runtime_src \
    && cp -r live_ingestion /build/live_src/live_ingestion \
    && cp live_pyproject.toml /build/live_src/pyproject.toml \
    && cp -r backfill_engine /build/backfill_src/backfill_engine \
    && cp backfill_pyproject.toml /build/backfill_src/pyproject.toml \
    && cp -r ingestion_runtime /build/runtime_src/ingestion_runtime \
    && cp runtime_pyproject.toml /build/runtime_src/pyproject.toml

RUN pip wheel --constraint /build/constraints.lock --wheel-dir /wheels /build/live_src \
    && pip wheel --constraint /build/constraints.lock --wheel-dir /wheels /build/backfill_src \
    && pip wheel --constraint /build/constraints.lock --wheel-dir /wheels --find-links=/wheels /build/runtime_src

# ---- runtime: minimal image, non-root ---------------------------------------------
FROM base AS runtime

RUN groupadd --system --gid 10002 algoting \
    && useradd --system --uid 10002 --gid algoting --home-dir /home/algoting --create-home algoting

WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels \
        live-ingestion==0.1.0 backfill-engine==0.1.0 ingestion-runtime==0.1.0 \
    && rm -rf /wheels

ENV LOG_LEVEL=INFO \
    LIVE_HEARTBEAT_PATH=/tmp/live_ingestion_heartbeat \
    BACKFILL_DB_PATH=/data/backfill_coverage/market_data.sqlite3

RUN mkdir -p /data/backfill_coverage && chown -R algoting:algoting /data /home/algoting
VOLUME ["/data"]

USER 10002:10002

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "ingestion_runtime.healthcheck"]

ENTRYPOINT ["python", "-m", "ingestion_runtime.live_runner"]
