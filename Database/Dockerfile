FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ---- builder: build a wheel + resolve deps in an isolated layer -----------------
FROM base AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY market_data_db ./market_data_db
COPY server ./server
RUN pip wheel --wheel-dir /wheels ".[server]"

# ---- runtime: minimal image, non-root, read-only-friendly -----------------------
FROM base AS runtime

RUN groupadd --system --gid 10001 algotdb \
    && useradd --system --uid 10001 --gid algotdb --home-dir /home/algotdb --create-home algotdb

WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels market-data-db[server] \
    && rm -rf /wheels

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV MARKET_DATA_DB_ROOT=/data \
    HOST=0.0.0.0 \
    PORT=8000

RUN mkdir -p /data && chown -R algotdb:algotdb /data /app
VOLUME ["/data"]

USER algotdb
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=3).status==200 else 1)" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
