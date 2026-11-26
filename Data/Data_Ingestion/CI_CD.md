# Ingestion CI/CD status and enablement

The root `.github/workflows/ingestion-pipeline.yml` runs mocked CI, data validation, security, image build and a lightweight benchmark, then requires the aggregate quality gate. The existing independent database implementation and CI configuration were not changed.

## Current release posture

`INGESTION_RELEASE_ENABLED` is intentionally unset, so delivery cannot publish a release. Staging, production and recovery likewise require explicit enablement variables and dedicated self-hosted runners. This is necessary because no deployment host, isolated staging database, provider test credentials, or approved deployment script was supplied. The release workflow currently lacks a completed Trivy image scan, Syft SBOM, Cosign signature, attestations, and published digest verification. Do not set the release variable until these controls are implemented and verified. The staging and production workflows reference `/opt/algot-ingestion/deploy.sh` as an infrastructure contract; that script is not present or tested here.

## Required GitHub configuration

- Protect the default branch and require the `quality-gate` job. Require review by CODEOWNERS where supported.
- Keep GHCR package private. Grant the release job `packages: write` only when publication is enabled.
- Use separate `ingestion-staging` and `ingestion-production` environments and self-hosted runners on isolated hosts. The production environment needs an explicit reviewer approval if the GitHub plan supports it; otherwise keep production disabled.
- Use separate environment secrets for provider credentials and market database API keys. Never put them in repository variables or PR workflows. Staging must use a staging database and strictly budgeted test symbols.
- For explicit production approval, set `INGESTION_PRODUCTION_APPROVED_SHA` to the exact release commit SHA for one promotion, then remove it. Enablement variables are `INGESTION_RELEASE_ENABLED`, `INGESTION_STAGING_ENABLED`, `INGESTION_PRODUCTION_ENABLED`, `INGESTION_RECOVERY_ENABLED`. Defaults are disabled.
- The backfill coverage SQLite volume `/data` is operational state. Keep it across replacements and rollback. Do not remove the market database or this volume during recovery.

## Local verification performed

- `Backfill/.venv/bin/python -m pytest -q Backfill/backfill_engine/tests ingestion_runtime/tests`: 52 passed.
- `Live/.venv/bin/python -m pytest -q Live/live_ingestion/tests`: 34 passed.
- `python3 ci/policy.py`: passed.
- `/tmp/actionlint -config-file ../.github/actionlint.yaml ../.github/workflows/ingestion-*.yml`: passed.
- `docker build -t algot-ingestion-ci-local .`: passed using the local Docker daemon.

These results do not establish remote GitHub workflow success, live provider compatibility, staging readiness or production deployment safety.
