# CI/CD configuration: secrets, variables, permissions

Everything GitHub must hold for the OCI, Terraform and Kubernetes pipelines,
and what is still outstanding. Nothing in this file is a credential; it
records what each credential is for and which workflows read it.

The guiding rule is that **no workflow triggered by an untrusted pull request
holds any of this**. Credentialed workflows are `push`-to-`main`,
`schedule` or `workflow_dispatch` only, and each one runs under a GitHub
environment so the credential can be scoped and the run can require a
reviewer.

## Repository secrets

Seven secrets, all OCI. There is no registry password, no application
credential and no database password in GitHub: the container registry token,
the cosign signing key and the application secrets all live in OCI Vault and
are fetched at run time using the API key below. That keeps the blast radius
of a GitHub compromise to "can call OCI as this one API user", which the IAM
policy constrains further.

| Secret | What it is | Read by |
|---|---|---|
| `OCI_PRIVATE_KEY` | PEM body of the API signing key for the CI user. Written to `~/.oci/api_key.pem` at `umask 077` and removed in an `always()` step. | infrastructure-plan, infrastructure-apply, infrastructure-drift, arm64-images, data-platform-deploy, data-platform-rollback |
| `OCI_USER_OCID` | OCID of the CI IAM user that key belongs to. | as above |
| `OCI_TENANCY_OCID` | Tenancy OCID. | as above |
| `OCI_FINGERPRINT` | Fingerprint of the uploaded public API key. | as above |
| `OCI_COMPARTMENT_OCID` | Compartment holding every AlgoT resource. | infrastructure-plan, infrastructure-apply, infrastructure-drift, arm64-images |
| `OCI_VAULT_OCID` | Vault holding the OCIR auth token, the cosign key and its password, and the application secrets. | arm64-images, data-platform-deploy |
| `GITHUB_TOKEN` | Provided automatically; never configured. Used read-only for gate re-checks and to raise the drift issue. | infrastructure-drift, data-platform-deploy, infrastructure-apply |

The CI user needs only: read the compartment's resources, manage the
resources Terraform declares, read the named Vault secrets, push to the
`algot-*` OCIR repositories, and add itself temporarily to the OKE API
access group (see `deployment/scripts/ci-api-access.sh`). It must **not** be
an administrator. `docs/infrastructure/ACCESS.md` records the policy
statements.

## Repository variables

Variables are deliberately used for the on/off switches rather than secrets,
because their values are not sensitive and seeing them set is the point.

### Deployment switches — all default to off

Every one of these must be set explicitly before the corresponding workflow
will do anything. This is the "explicit authorisation for subsequent
production deployments and infrastructure changes" requirement: there is no
path from a merge to applied infrastructure or a deployed image.

| Variable | Set to `true` to allow | Workflow |
|---|---|---|
| `INFRASTRUCTURE_APPLY_ENABLED` | `terraform apply` against the live tenancy | infrastructure-apply |
| `DATA_PLATFORM_DEPLOY_ENABLED` | deploying to, and rolling back in, `algot-data` | data-platform-deploy, data-platform-rollback |
| `DATABASE_RELEASE_ENABLED` | promoting a database image to private GHCR | database-release |
| `INGESTION_RELEASE_ENABLED` | promoting an ingestion image to private GHCR | ingestion-release |
| `GHCR_PRIVATE_CONFIRMED` | acknowledging GHCR packages are private | database-release, ingestion-release |
| `STAGING_DEPLOY_ENABLED` | staging deployment on a self-hosted runner | database-staging, ingestion-staging |
| `PRODUCTION_DEPLOY_ENABLED` | production deployment on a self-hosted runner | database-production, ingestion-production |
| `SYSTEM_RELEASE_ENABLED` | coordinated two-service deployment | system-release |
| `SYSTEM_RECOVERY_ENABLED` | system recovery to a previous manifest | system-recovery |
| `INGESTION_RECOVERY_ENABLED` | ingestion-only recovery | ingestion-recovery |

In addition, each dispatch requires its confirmation phrase typed exactly:
`APPLY` for an infrastructure apply, `DEPLOY` for a deployment, `ROLLBACK`
for a rollback. A mistyped phrase fails the run before any credential is
written.

### Settings

| Variable | Default if unset | Meaning |
|---|---|---|
| `OCI_REGION` | `uk-london-1` | Home region for every OCI call. |
| `OCIR_REGISTRY` | `lhr.ocir.io` | Registry endpoint; must match the region. |
| `API_ALLOWED_CIDRS` | `[]` | JSON list of CIDRs allowed to reach the OKE public API endpoint. Empty means nothing is allowed in, which is the safe default but will lock operators out until set. |
| `REGISTRY_STORAGE_BUDGET_GB` | `5` | OCIR storage ceiling. arm64-images fails when total billable image storage exceeds it, because image storage is **not** part of Always Free. |
| `PRODUCTION_APPROVED_DATABASE_DIGEST` | unset | Exact digest production is permitted to run; the amd64/GHCR chain only. |
| `PRODUCTION_APPROVED_INGESTION_DIGEST` | unset | As above. |

## Environments

Create these in Settings → Environments. The environment is what makes
"explicit authorisation" enforceable rather than advisory, so each one
should have **required reviewers** configured.

| Environment | Used by | Should require a reviewer |
|---|---|---|
| `infrastructure-plan` | infrastructure-plan, infrastructure-drift | No — read-only, and drift runs on a schedule |
| `production-infrastructure` | infrastructure-apply | **Yes** |
| `production-data` | data-platform-deploy, data-platform-rollback | **Yes** |
| `database-release`, `ingestion-release` | the release workflows | **Yes** |
| `staging`, `production` | the staging/production deployment workflows | **Yes** for production |

If the repository's GitHub plan does not offer environment reviewers, leave
the corresponding enable variable unset and treat the variable itself as the
authorisation gate: flipping it is then the deliberate act, and it is
visible in the audit log.

## Workflow permissions

The default for the repository should be **read-only** (Settings → Actions →
Workflow permissions). Every workflow then declares what it needs:

| Permission | Where, and why |
|---|---|
| `contents: read` | Every workflow. The only permission most of them have. |
| `actions: read` | data-platform-deploy, infrastructure-apply. Re-checks that the mandatory gates passed for the commit being deployed. Read-only. |
| `issues: write` | infrastructure-drift only, to open and update the single drift issue. |
| `packages: write` | arm64-images only, for the GHCR attestation path. OCIR itself is authenticated from Vault, not with this token. |
| `id-token: write`, `attestations: write` | arm64-images only, required by `actions/attest-build-provenance`. |

No workflow requests `contents: write`. Nothing in CI commits to the
repository.

## What untrusted pull requests can reach

These workflows run on `pull_request`, including from forks, and hold **no**
secrets, make **no** OCI API calls and touch **no** cluster or database:

- `infrastructure-validate` — terraform fmt/validate, Checkov, ShellCheck,
  free-tier policy unit tests, tracked-credential-file check
- `kubernetes-validate` — kubeconform schema, cost and data-protection
  invariants
- `kubernetes-security` — RBAC/token/network/hardening invariants, Checkov,
  Trivy config, and a restricted Pod Security Admission dry run on a
  throwaway `kind` cluster local to the runner
- `database-migration-validate` — the schema chain, against throwaway SQLite
  files
- `monorepo-pipeline` and the service pipelines — unit tests, lint, SAST,
  dependency audit, container build without push

Terraform `plan` deliberately does **not** run on pull requests. A plan needs
credentials, and `pull_request_target` would hand fork code those
credentials, so the trade-off is accepted: a plan is produced on push to
`main` instead, and the apply re-plans before applying.

## Free allowance notes

- The repository is **public**, so Actions minutes are unmetered and the
  `ubuntu-24.04-arm` native arm64 runners are free. Making the repository
  private would start billing both, and would make the arm64 build the
  single largest consumer.
- OCIR image storage is **not** Always Free. `REGISTRY_STORAGE_BUDGET_GB`
  caps it and `deployment/scripts/registry-gc.sh --apply` reclaims
  superseded manifests. Read the referrers warning in
  `docs/ci-cd/ARCHITECTURE.md` before running the GC.
- Block Volume **backup** storage is not Always Free either, which is why
  `oci_core_volume_backup_policy_assignment` is on the free-tier gate's
  denied list and backups go to Object Storage instead.
- `infrastructure-drift` runs daily. On a public repository that is free; it
  is the one scheduled workflow, chosen over an hourly check for that reason.

## Remaining configuration, and what has not been verified

Configuration still to be done by an operator:

1. Create the seven repository secrets and the environments above, with
   required reviewers on the production ones.
2. Set `API_ALLOWED_CIDRS` to the operator CIDR, or the OKE API endpoint
   stays unreachable.
3. Set the enable variables only when each path has been exercised.
4. Protect `main` and require the `quality-gate` status check.
5. Populate `deployment/manifests/approved.json` before using the
   amd64/GHCR release path; it is intentionally empty and rejects
   deployment today.
6. Review the scanner exceptions in `deployment/security/exceptions.json`
   before they expire. CI fails on an expired entry, so these are deadlines
   rather than suggestions:

   | Expires | Scanner | Finding | What to check |
   |---|---|---|---|
   | 2027-01-31 | gitleaks | `.env.example` generic-key false positive | Still a false positive |
   | 2027-03-31 | trivy-image | `CVE-2026-76642` (`util-linux`) | Whether Debian 13 has published a fixed `util-linux`; if so, drop the entry and rebuild |
   | 2027-03-31 | trivy-config | `KSV-0053` (`pods/exec`) | Whether the operational scripts still need `pods/exec`, and that every pod still sets `automountServiceAccountToken: false` |

Not yet verified, and not claimed:

- No credentialed workflow has run on GitHub. `infrastructure-plan`,
  `infrastructure-apply`, `infrastructure-drift`, `arm64-images`,
  `data-platform-deploy` and `data-platform-rollback` have passed Actionlint
  and their scripts pass ShellCheck, but they cannot run until the secrets
  exist. Their OCI, Kubernetes and Vault interactions are therefore
  unproven in CI, even though the equivalent steps have been run by hand
  (see the ARM64/OCIR section of `docs/ci-cd/ARCHITECTURE.md`).
- The staging and production self-hosted runners do not exist.
- Database migration **validation** is covered; a migration that actually
  changes the live schema has never been applied, because `MIGRATIONS`
  still contains only the baseline.
- `data-platform-rollback` has never been exercised against the cluster.
