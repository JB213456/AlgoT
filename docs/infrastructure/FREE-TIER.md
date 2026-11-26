# Always Free compliance

The hard requirement is **£0.00 recurring cost**. This document records the
allowance accounting, how it is enforced automatically, and how to verify
actual spend.

## This tenancy is pay-as-you-go, not a free-tier-only account

Verified before provisioning:

```
standard-a1-core-count    limit 250   (an Always Free-only account shows 4)
standard-a1-memory-count  limit 1666  (an Always Free-only account shows 24)
```

This matters. In an Always Free-only tenancy, Oracle refuses to provision
beyond the allowance — the account itself is the guard rail. In a PAYG
tenancy **nothing stops an over-allocation from billing**. The compliance
gate is therefore the only thing preventing charges, which is why it fails
closed.

Always Free allowances still apply to PAYG tenancies, so staying inside them
costs nothing.

## Ampere A1 allowance in this paid tenancy

Oracle's generic [Free Tier page](https://www.oracle.com/cloud/free/) lists
1,500 OCPU-hours and 9,000 GB-hours for Always Free accounts. Its
[UK price list](https://www.oracle.com/uk/cloud/price-list/) separately states
that **each paid tenancy** gets 3,000 OCPU-hours and 18,000 GB-hours monthly
at no charge. This is a pay-as-you-go tenancy, so the latter published
allowance applies. The allocation of 4 OCPUs and 24 GB for a 744-hour month
uses **2,976 OCPU-hours and 17,856 GB-hours**, leaving 24 and 144 respectively.

Observed A1 usage in this tenancy also had zero computed cost:

| Window | A1 usage | Cost |
|---|---|---|
| 1–24 Nov (previous project) | 1,879 OCPU-h, 11,276 GB-h | **£0.00** |
| 18–24 Nov (this cluster) | 576 OCPU-h, 3,456 GB-h | **£0.00** |

576 OCPU-hours over 6 days is exactly 4 OCPUs running continuously, billed at
zero. The price list establishes the paid-tenancy entitlement; zero historic
cost alone would not establish it.

**This is monitored, not assumed.** `deployment/scripts/check-costs.sh`
reports the A1 SKUs on every run and fails if they ever become non-zero.

**Contingency, if Oracle changes the paid-tenancy allowance.** Reduce the
pool to two 1 OCPU / 6 GB nodes after a reviewed maintenance plan:

```hcl
# infrastructure/terraform/terraform.tfvars
node_ocpus      = 1
node_memory_gbs = 6
```

That gives 2 OCPUs / 12 GB total, inside the current published allowance.
Storage is unaffected (boot volumes stay 50 GB each). The data services
reserve only 475m CPU, so they still fit comfortably.

## Allocation

| Resource | Allowance | Allocated | Remaining |
|---|---|---|---|
| Ampere A1 OCPU-hours, 744-hour month | 3,000 | **2,976** (2 nodes × 2 × 744) | 24 |
| Ampere A1 GB-hours, 744-hour month | 18,000 | **17,856** (2 × 12 × 744) | 144 |
| Block + boot storage | 200 GB | **180 GB** | **20 GB** |
| ├─ boot volumes | | 100 GB (2 × 50 GB) | |
| └─ database volume | | 80 GB | |
| Object Storage | 20 GB | state + backups (small) | ~20 GB |
| Vault secrets | 150 | 6 | 144 |
| Vault software keys | unlimited | 1 | — |
| Container Registry | up to 500 GB | measured from OCIR | see usage audit |
| OKE Basic clusters | 1 (tenancy limit) | 1 | 0 |

### Why ingestion checkpoints have no block volume

**OCI's minimum block volume size is 50 GB.** With 100 GB of boot volumes and
the 80 GB database volume already committed, a dedicated checkpoint volume
would bring the total to **230 GB — a 30 GB breach**. The specified
allocation (100 + 80 + 20 reserved) leaves no room for one.

So the backfill coverage store lives on an `emptyDir` that an init container
restores from Object Storage at startup and a sidecar snapshots every five
minutes. This is *more* durable than a node-local volume, which would be lost
on node replacement and would pin ingestion to a single node. The coverage
store is derived state: losing it costs provider quota for re-downloads, not
market data.

## Container Registry

Oracle's [Free Tier page](https://www.oracle.com/cloud/free/) lists up to
500 GB of OCI Container Registry storage under Always Free Kubernetes, and the
`Container Image Storage` SKU recorded 0.89 units at £0.00 when this
deployment was first provisioned.

**That is no longer what the bill says.** Audited 25 November 2026:

| Day | Quantity | Cost |
|---|---|---|
| 24 Nov 2026 | 0.0079 | £0.000160 |
| 25 Nov 2026 | 0.0095 | £0.000194 |

It is the only SKU in the tenancy still recording a non-zero recurring
amount. At roughly £0.0002/day it is about **£0.07 a year** and rounds to
£0.00 on an invoice, but it is not structurally zero the way `Standard - A1`
and `Block Volume - Free` are — those have their own zero-rated SKUs, and
registry storage does not.

Three options, in order of preference:

1. **Accept and watch it.** `check-costs.sh` already reports the SKU, and
   `registry-gc.sh` keeps the stored bytes near the minimum. This is the
   current choice.
2. **Prune harder.** Most of the 0.9 GB is superseded manifests. Running
   `registry-gc.sh --apply` after every build reduces the quantity but not to
   zero — the current images have to be stored somewhere.
3. **Move images back to GHCR**, which is free for public and private
   packages. This reverses a deliberate decision: images live in OCIR so that
   nodes pull them over the service gateway as instance principals, with no
   registry credential stored anywhere. Paying £0.07/year is the better trade.

If a strict zero is required, option 3 is the only one that achieves it, and
the cost is a stored registry credential.

## Vault: fully established

From Oracle's Always Free resources page, verbatim:

- *"150 total Always Free secrets (across any number of vaults)"*
- *"All master encryption keys protected by software are free"*
- *"virtual private vaults are not included in the Always Free resources"*

This deployment holds 8 secrets and two software-protected AES keys — one for
the secrets, one for encryption at rest of the volume and buckets — in a
`DEFAULT` (shared virtual) vault. The tenancy's own limits corroborate the
exclusion: `virtual-vault-count` is 10 while `virtual-private-vault-count`
is **0**, so the billable vault type cannot be created here at all.

## Automated enforcement

`infrastructure/terraform/policies/free-tier/check_plan.py` runs against
`terraform show -json` output and **fails closed**: a resource type whose free
eligibility has not been established in `allowlist.yaml` is a violation, not a
warning.

It rejects:

- any resource type not explicitly established as free-eligible
- explicitly billable types (load balancers, File Storage, virtual node pools,
  volume backup policies, OCI Monitoring alarms, Logging, reserved public IPs)
- compute shapes other than `VM.Standard.A1.Flex`
- `ENHANCED_CLUSTER`
- totals above 4 OCPUs, 24 GB memory, or 200 GB storage
- block volumes above 10 VPUs per GB (higher tiers bill Performance Units)
- node pool cycling, which transiently doubles node count past the allowance
- plans that cannot determine sizing at plan time
- public backup buckets
- **public container repositories** (private images are the point of OCIR)
- `VIRTUAL_PRIVATE` vaults, which bill per hour
- HSM-protected keys, capped at 20 free versions tenancy-wide
- **any plan that destroys or replaces the database volume**
- any plan that destroys the cluster, unless `--allow-cluster-replacement` is
  passed deliberately

It measures totals from the plan's **full planned state**, not just the
resources being changed. An incremental plan that adds one bucket would
otherwise report 0 OCPUs and pass trivially; instead it still sees the whole
4 OCPU / 24 GB / 180 GB allocation and checks it against the ceilings.

Second and third layers: Terraform `validation` blocks and `precondition`
assertions fail the plan before any API call, and the namespace
`ResourceQuota` pins `services.loadbalancers` and `services.nodeports` to `0`
so a billable Service cannot be created even by hand.

Run the gate's own tests with:

```bash
pytest infrastructure/terraform/policies/free-tier/ -v
```

They assert the gate *rejects* each violation class; a gate that only ever
passes is worthless.

## Verifying actual spend

The allocation being free is a claim about configuration. This is the claim
about money:

```bash
oci usage-api usage-summary request-summarized-usages \
  --tenant-id "$(terraform -chdir=infrastructure/terraform output -raw tenancy_ocid)" \
  --granularity DAILY --query-type COST \
  --group-by '["service","skuName"]' \
  --time-usage-started "$(date -u -d '2 days ago' +%Y-%m-%dT00:00:00Z)" \
  --time-usage-ended   "$(date -u +%Y-%m-%dT00:00:00Z)"
```

Expect `Compute — Standard - A1`, `Standard - A1 - Memory` and
`Block Volume - Free` to report quantities with **`computed-amount` 0.0**.
A non-zero amount against any SKU means something outside the envelope was
created.

SKUs that would indicate a problem: `Standard - E*` (a paid shape),
`Block Volume - Performance Units` (VPUs above the free tier),
`Block Volume - Backup` (a backup policy), or any `Load Balancer` line.

### Audit of 25 November 2026

Every one of those problem SKUs had in fact appeared during November, from
work that predates the current deployment. All had stopped by the time of the
audit; the figures are recorded here so a future reading of the month's
invoice is not mistaken for a live problem.

| SKU | November total | Status on 25 Nov |
|---|---|---|
| `Block Volume - Backup` | £0.4423 | Stopped. Last partial charge 24 Nov; no volume or boot-volume backup exists in either compartment, and no backup policy is assigned. |
| `Compute — Standard - E4` | £0.8694 | Stopped. 43.5 hours of a paid AMD shape. No E4 instance exists in the tenancy. |
| `Block Volume - Storage` (paid portion) | £0.2402 | Stopped. Residual from boot volumes of the two earlier, now-deleted clusters. |
| `Block Volume - Performance Units` | £0.1601 | Stopped. Same cause. |
| `Container Image Storage` | £0.0004 | **Continuing** — see Container Registry above. |
| `Object Storage - Storage` | £0.0000003 | Continuing, at 1.3e-05 GB-month. Rounds to zero at any horizon. |

On 25 November the tenancy's block storage sat entirely under the zero-rated
`Block Volume - Free` SKU, and both A1 SKUs recorded £0.00 against 4 OCPUs and
24 GB running continuously.

Entitlement was re-verified the same day: `standard-a1-core-count` 250 with 4
used, `standard-a1-memory-count` 1666 with 24 used,
`container-engine/cluster-count` 1, and `kms/virtual-private-vault-count` 0.
This confirms the PAYG-with-Always-Free picture described at the top of this
document, and confirms that **the tenancy will not stop an over-allocation** —
only the gate will.

## Guard rails worth keeping

- Never raise `node_count`, `node_ocpus`, `node_memory_gbs` or
  `database_volume_gbs` without re-running the gate. The variable
  `validation` blocks catch the obvious cases; the gate catches the totals.
- Never set `allowVolumeExpansion: true` on the StorageClass. Expanding the
  volume past the allowance would bill silently.
- Keep the `algot-bv-retain` StorageClass on `kubernetes.io/no-provisioner`.
  If it named the real CSI provisioner, a static PV that failed to match
  would cause Kubernetes to dynamically provision a **second** 80 GB volume
  (260 GB total, a 60 GB breach). With no provisioner, a mismatch leaves the
  claim `Pending` — a visible failure rather than a silent charge.
- Keep `api_allowed_cidrs` narrow. It costs nothing, but a public API
  endpoint is a security problem, not a billing one.
- The cluster count limit is **1**. A plan that replaces the cluster must
  destroy it first; there is no blue/green path.
- The gate also refuses to **replace the worker node pool**. At 4 OCPUs there
  is no headroom for a surge node, so replacing the pool recycles both
  workers at once: an outage, not a rolling change. Override with
  `--allow-node-pool-replacement` only after checking that the attribute
  forcing replacement cannot be changed in place.
- Never attach a volume backup policy. `Block Volume - Backup` is not in any
  free allowance and bills from the first byte; it was the largest charge this
  tenancy has incurred. Both `oci_core_volume_backup_policy_assignment` and
  `oci_core_volume_backup` are on the gate's denied list, and Checkov's
  CKV_OCI_2 (which asks for exactly this) is skipped with that reason
  recorded in `.checkov.yaml`.
