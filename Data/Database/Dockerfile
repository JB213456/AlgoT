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

# ---- builder: build a wheel + resolve deps in an isolated layer -----------------
FROM base AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential=12.12 \
    && rm -rf /var/lib/apt/lists/*
COPY constraints.lock ./
COPY pyproject.toml ./
COPY market_data_db ./market_data_db
COPY server ./server
RUN pip wheel --constraint constraints.lock --wheel-dir /wheels ".[server]"

# ---- runtime: minimal image, non-root, read-only-friendly -----------------------
FROM base AS runtime

RUN groupadd --system --gid 10001 algotdb \
    && useradd --system --uid 10001 --gid algotdb --home-dir /home/algotdb --create-home algotdb

WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels 'market-data-db[server]==0.1.0' \
    && rm -rf /wheels

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV MARKET_DATA_DB_ROOT=/data \
    HOST=0.0.0.0 \
    PORT=8000

RUN mkdir -p /data && chown -R algotdb:algotdb /data /app
VOLUME ["/data"]

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health',timeout=3)"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
