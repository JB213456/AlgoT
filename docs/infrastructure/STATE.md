# Terraform state

State for the AlgoT OCI deployment is held **locally** at
`infrastructure/terraform/terraform.tfstate` and synchronised to a private,
versioned OCI Object Storage bucket by
[`deployment/scripts/terraform-state.sh`](../../deployment/scripts/terraform-state.sh).

```
infrastructure/terraform/terraform.tfstate      working copy
oci://algot-terraform-state/algot/terraform.tfstate     authoritative copy
oci://algot-terraform-state/algot/terraform.tfstate.lock advisory lock
```

## Why not a Terraform backend

OCI has no first-party Terraform backend. The usual workaround is the `s3`
backend against Object Storage's S3-compatible endpoint, which authenticates
with a **Customer Secret Key** — a long-lived HMAC credential, separate from
the operator's API signing key, that has to be created, stored and rotated by
hand.

Syncing with the script keeps the operator's existing API key as the only
credential in the system. The cost is that the sync is explicit: `pull` before
planning, `push` after applying. Both the plan and apply workflows do this
automatically; working locally, do it by hand.

## Usage

```bash
deployment/scripts/terraform-state.sh pull      # before planning
deployment/scripts/terraform-state.sh lock      # before applying
deployment/scripts/terraform-state.sh push      # after applying
deployment/scripts/terraform-state.sh unlock    # release the lock
deployment/scripts/terraform-state.sh versions  # recoverable versions
deployment/scripts/terraform-state.sh restore <version-id>
```

`pull` never silently overwrites a local file that differs from the remote: it
keeps the local copy alongside as `terraform.tfstate.local.<timestamp>` and
says so. If that happens, work out which copy is newer before continuing —
`serial` inside the file increases with every apply.

## The lock

`lock` is an **advisory** lock, enforced by a conditional create: the script
PUTs the lock object with an `If-None-Match: *` header, so Object Storage
returns `412 Precondition Failed` if the object already exists. Only one
holder can create it.

It is advisory in that nothing forces a Terraform run to take it — it protects
against two operators, or an operator and CI, applying at once, provided both
go through the script. The apply workflow takes it; the plan and drift
workflows deliberately do not, because they only read.

A crashed run can leave the lock behind. `lock` prints the holder, host and
time it was taken, so stale locks are identifiable; release one with `unlock`.

> This is the operation that justifies the script's use of `oci raw-request`.
> `oci os object put` has no `--if-none-match` option, so an earlier version
> of the script failed on every invocation and reported the state as locked by
> another operator when no lock object existed at all.

## Protection

**Versioning.** The bucket has versioning enabled, so every `push` is
recoverable. `versions` lists them and `restore` fetches one. After a restore,
run `terraform plan` and confirm it is empty before applying anything.

**Encryption.** The bucket is encrypted with a customer-managed key,
`algot-storage-key`, in the `algot-vault` Vault — not with Oracle's default
key. The same key encrypts the `algot-backups` bucket and the market data
block volume. Software-protected keys are free, so this costs nothing.

The Object Storage service principal is granted `use keys` by the
`algot-service-encryption-policy` IAM policy, which allows encrypt and decrypt
only. Neither Object Storage nor Block Volume can rotate, disable or delete
the key.

> **The key and the state depend on each other.** State is stored under a key
> that Terraform itself manages. If the key were destroyed, the state would be
> unreadable and the recovery path would go with it. Both the Vault and the
> key therefore carry `prevent_destroy`, and OCI only ever *schedules* a key
> deletion — a minimum of 7 days, cancellable — so there is a window to
> recover. Do not disable the key.

**Secrets in state.** No secret *values* are in state: `oci_vault_secret`
resources are deliberately not managed by Terraform, because their contents
would be recorded in the state file in plain text. Secrets are written
directly to the Vault API by
[`vault-secrets.sh`](../../deployment/scripts/vault-secrets.sh).

State does still contain every resource attribute, so it is treated as
sensitive: `*.tfstate` is git-ignored, the bucket is `NoPublicAccess`, and so
is `terraform show -json` output (`infrastructure/terraform/*.json` is
git-ignored too, since the plan workflow writes `plan.json` there).

## Recovering from a lost or corrupted state file

1. `deployment/scripts/terraform-state.sh versions`
2. `deployment/scripts/terraform-state.sh restore <version-id>`
3. `terraform -chdir=infrastructure/terraform plan` — it must be **empty**.

If no usable version exists, the deployment can be rebuilt into state by
importing rather than recreating. Every resource's OCID is discoverable from
the OCI console or CLI, and
[`drift-detect.sh unmanaged`](../../deployment/scripts/drift-detect.sh) lists
everything in the compartment that the current state does not cover:

```bash
terraform -chdir=infrastructure/terraform import <address> <ocid>
```

Import, never re-apply from empty state: a fresh apply would try to create a
second OKE cluster against a tenancy limit of one, and would replace the
market data volume.
