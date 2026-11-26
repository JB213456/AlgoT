# Minimal maintenance image: OCI CLI + curl + python, nothing else.
#
# Used by two workload groups that must talk to OCI Object Storage or the
# database API, none of which belongs in the application images:
#   - the ingestion pod's checkpoint restore/snapshot containers
#   - the weekly database backup CronJob
#
# Authentication is always an OCI instance principal, so this image contains
# no credentials and no OCI config file.
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

FROM base AS builder
WORKDIR /build
COPY requirements.txt ./
RUN pip wheel --wheel-dir /wheels -r requirements.txt

FROM base AS runtime

# curl for the database API calls; ca-certificates so TLS verification works.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        # Exact version available in the digest-pinned Debian trixie base.
        curl=8.14.1-2+deb13u7 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
COPY requirements.txt ./
RUN pip install --no-compile --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels requirements.txt

# The pods that use this image set runAsUser to 10001 or 10002 to match the
# volume they mount, so no single uid is baked in. HOME is set per-workload
# to a writable emptyDir because the root filesystem is read-only.
USER 10002:10002

# No ENTRYPOINT: every caller supplies an explicit script path, which keeps
# this image from being runnable as an ambient shell.
CMD ["python3", "-c", "import oci, sys; print('algot toolbox', oci.__version__, 'python', sys.version.split()[0])"]
