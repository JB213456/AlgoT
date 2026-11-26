# Access, state and credentials

## Cluster access

```bash
deployment/scripts/setup-kubeconfig.sh
export KUBECONFIG=~/.kube/algot-oke.config
kubectl get nodes
```

The generated kubeconfig holds **no long-lived token**: it invokes the OCI
CLI for a short-lived exec credential on each call, so the operator's API key
remains the only secret. It is written to `~/.kube/`, outside the repository,
and `.gitignore` covers the filename regardless.

## Kubernetes API exposure

The API endpoint is public but its NSG permits only the CIDRs in
`api_allowed_cidrs` on 6443. The default is an **empty list**, which creates
no ingress rule at all — the endpoint is unreachable until an address is
added deliberately.

A private endpoint was considered and rejected: reaching it requires either
OCI Bastion or a jump host, and neither is inside the established free
envelope. A public endpoint restricted to a single `/32`, combined with TLS,
RBAC and short-lived exec credentials, is the better trade here.

### Why CI does not sit in the allowlist

GitHub-hosted runners draw from thousands of published ranges — far more than
an NSG holds — so permanently allowlisting them would mean effectively
opening 6443 to the internet. Instead `deployment/scripts/ci-api-access.sh`
grants the runner's own `/32` for the duration of a deployment and revokes it
afterwards, in an `if: always()` step. Rules it creates carry the description
`ci-temporary-kubectl-access`, and revoke removes only those, so an operator
rule is never withdrawn by accident.

## Terraform state

State is a local file synced to the private, versioned bucket
`algot-terraform-state`.

```bash
deployment/scripts/terraform-state.sh pull      # before planning
deployment/scripts/terraform-state.sh lock      # before applying
deployment/scripts/terraform-state.sh push      # after applying
deployment/scripts/terraform-state.sh unlock
deployment/scripts/terraform-state.sh versions
```

### Why not the S3-compatible backend

OCI's S3-compatible endpoint requires a **Customer Secret Key** — a second
long-lived credential, stored in CI, with standing write access to the state
bucket. Syncing with the operator's existing API key avoids creating it. The
bucket is versioned, so every push is recoverable; it is encrypted with the
customer-managed `algot-storage-key`; and `lock` uses a conditional object
create (`If-None-Match: *`) for mutual exclusion, so only one holder can
create the lock object.

That conditional create goes through `oci raw-request`, because
`oci os object put` has no `--if-none-match` option — an earlier version of
the script passed that flag and therefore failed on every call, reporting the
state as locked when no lock existed.

The trade-off is that locking is advisory rather than enforced by the
backend. The CI workflows serialise on a single
`concurrency: algot-terraform-state` group, which is the real protection
against concurrent applies. The plan and drift workflows deliberately take no
lock, because they only read.

See [STATE.md](STATE.md) for the full state layout, recovery procedure and the
key-dependency caveat.

## Secrets

Nothing credential-bearing is committed. `.gitignore` covers `.env`,
`*.pem`, `terraform.tfvars`, `*.tfstate`, kubeconfigs and rendered
manifests, and `infrastructure-validate.yml` fails the build if a
credential-bearing file is ever tracked.

Application credentials live in **OCI Vault**, not in GitHub and not in
`.env` at deploy time. `deployment/scripts/vault-secrets.sh push` seeds the
vault from the local `.env` files once; `sync` then creates the Kubernetes
Secrets from Vault. Values are streamed to the API and never echoed or
written to disk. `sync` also verifies that the ingestion client key is one of
the keys the database accepts — otherwise every write would fail with HTTP
401 only at runtime.

Vault secrets (5 of the 150 Always Free allowance):

| Vault secret | Becomes |
|---|---|
| `algot-market-data-db-api-keys` | `market-data-db-auth` |
| `algot-market-data-db-client-key` | `market-data-db-client` |
| `algot-alpaca-api-key`, `algot-alpaca-secret-key` | `alpaca-credentials` |
| `algot-lse-api-key` | `lse-credentials` |
| `algot-ocir-auth-token` | `ocir-pull` (private registry) |

Non-secret configuration such as `ALPACA_SYMBOLS` is plain environment in the
Deployment manifest, not a Secret.

| Secret | Contents | Consumed by |
|---|---|---|
| `market-data-db-auth` | `api-keys` | database |
| `market-data-db-client` | `MARKET_DATA_DB_API_KEY` | ingestion, backfill, maintenance |
| `alpaca-credentials` | `ALPACA_*` | ingestion, backfill |
| `lse-credentials` | `LSE_*` | backfill only |
| `ocir-pull` | private OCIR pull token | attached to the service accounts |

Workloads reach OCI through **instance principals**, so no OCI credential
exists inside any pod. The `algot-worker-nodes` dynamic group is scoped to
object writes in `algot-backups` plus volume attachment.

## CI credentials

Required repository secrets:

| Secret | Purpose |
|---|---|
| `OCI_TENANCY_OCID` | tenancy |
| `OCI_USER_OCID` | API user |
| `OCI_FINGERPRINT` | API key fingerprint |
| `OCI_PRIVATE_KEY` | API private key (PEM) |
| `OCI_COMPARTMENT_OCID` | target compartment |
| `OCI_VAULT_OCID` | vault holding the application secrets |

GitHub holds **no application credential and no registry password**. The five
OCI values above are the bootstrap identity CI needs to reach Oracle; every
Alpaca, London Strategic Edge, database and registry secret is fetched from
OCI Vault at run time.

To remove even the bootstrap credential, run the deployment from a
self-hosted runner on OCI and authenticate with an instance principal. The
repository's existing CD chain already assumes self-hosted runners
(`ingestion-staging`, `ingestion-production`), so that path is open; it is not
used here because all 4 OCPUs are allocated to the cluster.

Variables: `OCI_REGION`, `API_ALLOWED_CIDRS`, and the enablement switches
`INFRASTRUCTURE_APPLY_ENABLED` and `DATA_PLATFORM_DEPLOY_ENABLED`, both of
which default to disabled.

**No workflow triggered by `pull_request` holds any of these.**
`infrastructure-validate.yml` and `kubernetes-validate.yml` run on untrusted
pull requests and are static-analysis only — no OCI API calls, no cluster
access. Plan, apply and deploy run on `push` to `main` or
`workflow_dispatch`, behind GitHub environments that should be configured to
require reviewer approval.

If the API key is rotated, update `OCI_PRIVATE_KEY` and `OCI_FINGERPRINT`,
then re-run `setup-kubeconfig.sh` locally.

> The private key currently referenced by `~/.oci/config` has its passphrase
> stored in that file in plaintext. That is outside this repository and is
> the operator's to manage, but it is worth replacing with a passphrase-less
> key held at `0600`, or an encrypted key whose passphrase is supplied at use
> time.
