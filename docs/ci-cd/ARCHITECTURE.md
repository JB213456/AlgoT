# AlgoT CI/CD architecture

## Repository and workflow layers

Tracked service source now lives under `Data/Database` and `Data/Data_Ingestion`. The planned services have empty directories under `Core/`; their pipeline entry points fail closed when tracked source is added until service-specific checks are implemented. `Strategies/` remains excluded from Git. The lowercase `/data/` rule still excludes generated market data; `/Data/` is no longer ignored so the moved source can be tracked. The Docker build contexts are service directories, never the repository root.

`monorepo-pipeline.yml` detects affected services on PRs and default-branch pushes. Database interface changes select both Database and Ingestion; ordinary ingestion changes skip the Database image build. Shared workflow changes select active services. The dispatcher requires every selected service gate and the system integration gate to succeed. Manual dispatcher runs select both active services. Future service source changes select their fail-closed gate.

Shared reusable workflows handle CI, SAST and dependency audit, Docker build, container vulnerability scanning, SBOM generation, performance checks, publishing, and deployment. Ingestion retains its provider/data checks and additional Gitleaks, Hadolint, Checkov and Actionlint checks. System tests exercise the ingestion wire payload through the Database HTTP API with temporary storage and verify exact deduplication. No live provider traffic occurs in routine CI.

## Infrastructure and data-platform layers

`infrastructure-validate`, `kubernetes-validate`, `kubernetes-security` and
`database-migration-validate` are the credential-free gates. They run on
pull requests from forks, hold no secret, make no OCI call and touch no
cluster or database, which is what lets them be mandatory without handing
untrusted code anything.

`kubernetes-security` is the security counterpart to `kubernetes-validate`:
the latter asks whether a manifest is well formed and within the free tier,
the former asks what an attacker would get from a compromised pod. It runs
four independent jobs — the in-house RBAC/token/network/hardening
invariants, Checkov, Trivy config, and a server-side admission dry run on a
throwaway `kind` cluster whose namespace is labelled `restricted`. That last
one is the only check here with real authority on Pod Security: it asks an
actual API server. It ends with a control case that applies a privileged
root pod and requires it to be **rejected**, because a dry run against a
namespace that was not enforcing anything would otherwise pass every
manifest and look like success.

`database-migration-validate` gates the one deployment step no image
rollback undoes. Rolling the container back does not un-drop a column, so
the chain must be contiguous, append-only against the base revision, free of
destructive statements, and replayable from every historical version to
head. The append-only check compares against `git show <base>:…/migrator.py`
and parses it with `ast` rather than importing it, so it never executes code
from the revision under test.

Each gate's own test suite runs immediately before the gate does, and those
tests are mostly of the form "this gate REJECTS a wildcard Role" or "this
gate REJECTS a DROP TABLE". A gate that silently stopped working would
otherwise report a clean pass indefinitely, which is worse than no gate.

Deployment and infrastructure apply re-check that the mandatory gates
actually passed for the exact commit being deployed
(`deployment/scripts/require-green-gates.sh`), treating a gate that never ran
as a failure. Both are dispatch-only, so without this a dispatch could name
any ref and the gates would be advisory.

Persistent data protection is asserted rather than assumed.
`assert-data-protection.sh` records the bound volume's OCI OCID, the PV and
PVC identity, the reclaim policy and the row counts before a deploy, and
re-checks them afterwards and again after any rollback. The failure it exists
for is a deployment that rebinds the claim to a *different* volume: the
database then comes up healthy and empty, and every other check passes.

Rollback is per service. `data-platform-deploy` rolls back only the component
it deployed, and `data-platform-rollback` rolls one service back on request
and then proves the other service's image and spec generation are unchanged.
A bad ingestion image must not cost a restart of the pod holding the market
data volume.

Only `algot-data` is deployed into, and that is enforced:
`validate-k8s-manifests.py` rejects any namespaced object that targets
another namespace or omits `metadata.namespace` entirely, and rejects the
creation of any other namespace.

## Release path

Each service has an independent `database-vMAJOR.MINOR.PATCH` or `ingestion-vMAJOR.MINOR.PATCH` tag. A manually dispatched service release requires that tag, its exact commit SHA, and a successful default-branch monorepo CI run ID for that SHA. The release workflow downloads the image and SBOM from that run, promotes the same image to private GHCR, records the immutable digest, signs it with Cosign, and attaches build provenance. It never rebuilds the image. Release jobs require `DATABASE_RELEASE_ENABLED=true` or `INGESTION_RELEASE_ENABLED=true`, plus `GHCR_PRIVATE_CONFIRMED=true`; both default to disabled.

The manifest validator requires approved Database and Ingestion digests, semantic versions, commit SHAs, and the same declared interface version. `deployment/manifests/approved.json` is empty and intentionally rejects deployment. System deployment and recovery run Database before Ingestion, using an approved compatible manifest. Recovery remains disabled until a previous compatible manifest is populated and `SYSTEM_RECOVERY_ENABLED` is explicitly set. Service deployment uses a single-container lock, stops the previous container before starting its replacement, preserves named volumes and environment files, waits for health, and restores the previous container if replacement fails. It never removes market-data or checkpoint volumes.

Staging and production require dedicated self-hosted runners labeled `ingestion-staging` and `ingestion-production`. Runner hosts need Docker, `flock`, `/etc/algot/{staging,production}/{database,ingestion}.env`, and writable `/var/lib/algot-deploy`. Both services must use the same target network. Keep staging credentials and volumes separate from production. Set `STAGING_DEPLOY_ENABLED=true` only after isolated staging tests. Production also requires `PRODUCTION_DEPLOY_ENABLED=true`, environment approval where the GitHub plan supports it, and exact `PRODUCTION_APPROVED_DATABASE_DIGEST` or `PRODUCTION_APPROVED_INGESTION_DIGEST` variables. Clear approvals after promotion. `SYSTEM_RELEASE_ENABLED=true` is required for coordinated deployment. If environment reviewers are unavailable on the existing plan, leave production disabled and use an independent approval gate.

Protect `main` and require the monorepo `quality-gate` status check. Keep GHCR packages private. Do not grant provider or production database credentials to PR workflows. Use separate repository or environment secrets for release and deployment. The current release artefact retention is one day for candidate images and seven days for SBOMs; dispatch releases before the image artefact expires.

## Security blocker and local verification

Actionlint accepts all 34 workflow files. Database tests passed (18); live ingestion tests passed (34); backfill and runtime tests passed (52); system and deployment guard tests passed (15). Both images built locally, both Compose files validated, and the Database container returned a healthy readiness response. Semgrep ran 151 Python rules on 53 tracked files with zero findings. pip-audit found no known vulnerabilities in the checked Database and Ingestion requirements. Syft generated valid CycloneDX SBOMs for both local images. An official Python Bookworm base candidate reported more findings, so the Dockerfiles were not changed.

The **53 high-severity base-image findings** that previously blocked this gate turned out to be one CVE, not 53 problems: `CVE-2026-76642` in `util-linux`, which Trivy counts once per affected package. Debian 13 ships no patched `util-linux`, so the Fixed Version column is empty and there is nothing to upgrade to. It is now a documented, expiring exception rather than a permanent blocker — see the suppression policy below. Nothing in these containers invokes `mount`, they run as a non-root UID with ALL capabilities dropped, and the restricted Pod Security Standard forbids the privilege a mount helper would need.

## Scanner suppression policy

`deployment/security/exceptions.json` is the only place a scanner finding may be suppressed, across Gitleaks, Trivy image scanning and Trivy misconfiguration scanning. Every entry needs a reason and an `expires` date, and `validate-security-exceptions.py` fails CI on an entry that is expired, unjustified, or names an unknown scanner.

The ignore files the scanners read are **generated** from that record at scan time rather than committed beside it, so there is no second place to look and the two cannot drift. `.gitleaksignore` is the one exception, because the Gitleaks action reads it from the repository root and cannot be pointed elsewhere; it is therefore checked for drift against the record instead of generated.

Two properties are worth keeping in mind. `--ignore-unfixed` is deliberately **not** used on the amd64 image scan: it would silence every future unpatched CVE in advance, including ones that do affect this workload, whereas an ignore file names exactly what a human reviewed. And the manifest misconfiguration scan runs a second time with no ignores at all, asserting that the findings and the record correspond exactly — so a new finding fails, and so does an entry that has gone stale because a check was renamed upstream.

The expiry is the point. An unfixed CVE is suppressed because there is nothing to upgrade to *today*, and that stops being true without anyone noticing.

The release and deployment workflows have passed Actionlint, but they have not run on GitHub. Cosign signing, attestations, GHCR privacy, staging and production runners, provider authentication, database migration compatibility, provider-connected end-to-end ingestion, rollback on a real host, RTO and RPO are not verified. Keep release and deployment enablement variables unset until those checks and the base-image security blocker are resolved. The user confirmed that an isolated staging runner and database host are not yet available. No remote workflow, provider integration, production deployment or system recovery has been claimed as passed.

## ARM64 / OCIR image chain (verified 25 November 2026)

This is a separate chain from the amd64/GHCR release flow above, and unlike that one it has actually run end to end. `deployment/scripts/build-arm64-images.sh` published all three images to private OCIR repositories by digest, and every stage below was executed rather than only linted.

Verified for `algot-database`, `algot-ingestion` and `algot-toolbox`: `linux/arm64` manifest and a non-root `USER`; a smoke test of the image pulled back from the registry by digest; a Trivy scan with no HIGH/CRITICAL finding that has a fix available; a CycloneDX SBOM (2850, 2843 and 3039 components) committed under `deployment/manifests/sbom/`; a cosign signature plus CycloneDX and SLSA-provenance attestations, all re-verified independently against the committed `deployment/security/cosign.pub`. Both OKE worker nodes pulled all three digests from OCIR using the `ocir-pull` secret, confirmed by kubelet `Pulled` events, and ran them under the namespace's restricted Pod Security Standards.

Two fixable CVEs were found and fixed rather than suppressed, which is why the scan gate is worth having: `CVE-2026-124851` (HIGH, `libzstd1`) affected all three images and is patched by an explicit `libzstd1=1.5.7+dfsg-1+deb13u1` pin in each Dockerfile's `base` stage — the newest `python:3.12-slim` still ships the vulnerable version, so repinning the base does not help. `GHSA-6q48-6c7w-9gw2` (CRITICAL, `certifi` bundling compromised emSign root CAs) affected the toolbox image; `oci-cli` was moved from 3.95.1 to 3.97.1, which requires `certifi>=2026.10.19`. The older pin capped `certifi<2026.0.0` and could not take the fix.

Signing uses a cosign key held in OCI Vault rather than a keyless Sigstore identity, so one committed public key verifies an image whether it was built by an operator or by `arm64-images.yml`. `deploy-data-platform.sh` re-verifies every digest's signature before applying and refuses an unsigned image.

One trap is worth recording because it cost a signing round-trip. cosign 3.x stores signatures and attestations as OCI referrers: a tagged fallback index at `sha256-<subject-digest>` whose entries are *untagged* manifests. Deleting untagged manifests as garbage therefore destroys the signatures while leaving the index behind, after which verification fails against an image that is otherwise intact — and cosign cannot re-sign, because it tries to append to an index referencing manifests the registry no longer has. Recovery required deleting the stale index first. `registry-gc.sh` now reads every retained index and protects the manifests it points at, and that is covered by re-verifying all three signatures after an `--apply` run.

Before this refactor there were 11 ingestion-only workflow files. There are now 34 files covering shared, service and system layers. Path selection avoids unrelated service builds, and prevalidated system calls avoid rerunning both service suites. Actual GitHub minutes cannot be measured until remote runs occur.
