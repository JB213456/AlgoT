# AlgoT infrastructure

Oracle Cloud infrastructure for the AlgoT platform: an OKE Basic cluster on
two Ampere A1 worker nodes, provisioned by Terraform, running the
`algot-data` namespace.

**Every resource is Always Free eligible. Projected recurring cost: £0.00.**

## What is deployed

| Layer | Resource | Always Free basis |
|---|---|---|
| Network | VCN, 3 subnets, internet/NAT/service gateways, 3 NSGs | No charge for the virtual network or gateways |
| Kubernetes | OKE **Basic** cluster, v1.34.10 | Basic control plane is free; Enhanced bills per cluster-hour |
| Compute | 2 × `VM.Standard.A1.Flex`, 2 OCPU / 12 GB each | Within the 4 OCPU / 24 GB Ampere A1 allowance |
| Storage | 2 × 50 GB boot + 1 × 80 GB block volume | 180 GB of the 200 GB allowance |
| Object Storage | `algot-terraform-state`, `algot-backups` | Within the 20 GB allowance |
| Registry | 3 **private** OCIR repositories | Image storage billed £0.00 in this tenancy |
| Secrets | OCI Vault (`DEFAULT` type) + software key | 150 free secrets; software keys free |
| IAM | Scoped service, cluster and dynamic-group policies | No charge |

Deliberately absent, because each one bills: load balancers, Ingress,
NodePort Services, OCI File Storage, volume backup policies, OCI Monitoring
alarms, Logging, virtual nodes, reserved public IPs.

## Images and secrets live in Oracle, not GitHub

Container images are published to **private OCIR repositories**, and
application credentials live in **OCI Vault**. GitHub holds no registry
password and no application secret.

OCIR rather than GHCR because OCIR repositories are private by default and
cannot be made public by accident, whereas GHCR packages are created *public*
on first push and need an `admin:packages` token to change. Worker nodes also
pull from OCIR over the service gateway, on Oracle's own network.

OCI Vault rather than GitHub repository secrets because the Alpaca, London
Strategic Edge and database credentials are production trading credentials.
Pods read them with an instance principal, so no pod and no workflow holds a
static OCI credential for them.

```bash
deployment/scripts/vault-secrets.sh push     # seed/rotate Vault from .env
deployment/scripts/vault-secrets.sh sync     # create the Kubernetes Secrets
deployment/scripts/vault-secrets.sh list     # names and versions, no values
```

## Architecture

```text
                      operator (allowlisted /32)
                               │ 6443
                    ┌──────────▼──────────┐
                    │ OKE Basic control   │   public endpoint,
                    │ plane (free)        │   NSG-restricted
                    └──────────┬──────────┘
                               │
   VCN 10.0.0.0/16             │
   ├─ 10.0.0.0/28   k8s API subnet (public)
   ├─ 10.0.16.0/20  worker subnet (private)  ──► NAT ──► Alpaca, LSE (443)
   └─ 10.0.32.0/19  pod subnet (private, VCN-native pod IPs)
                              └────────► service gateway ──► OCIR, Object Storage
                                         (Oracle's network, not the internet)

   ┌─────────────── node 1 (AD-1) ───────────────┐  ┌──── node 2 (AD-1) ────┐
   │ 2 OCPU / 12 GB                              │  │ 2 OCPU / 12 GB        │
   │                                             │  │                       │
   │  algot-data namespace (PSS: restricted)     │  │  headroom reserved    │
   │   ├─ market-data-db (StatefulSet)           │  │  for algot-strategies │
   │   │    ClusterIP :8000, 80 GB block volume  │  │  algot-core           │
   │   └─ data-ingestion (Deployment)            │  │  algot-monitoring     │
   │        live Alpaca feed ──► database        │  │                       │
   └─────────────────────────────────────────────┘  └───────────────────────┘
```

Both nodes sit in **one availability domain** on purpose. OCI block volumes
are AD-scoped, so a same-AD pool lets the 80 GB database volume re-attach to
either node after a failure. Spreading the nodes across ADs would pin the
database permanently to whichever AD held its volume.

## Why VCN-native pod networking, not flannel

The cluster uses the OCI VCN-Native Pod Networking CNI rather than flannel.
flannel accepts `NetworkPolicy` objects and silently ignores them, and Oracle
**does not support** running the Calico policy engine alongside flannel —
their documentation states it "causes network issues". VCN-native pod
networking is the only OKE configuration in which Oracle supports Calico in
policy-only mode, and therefore the only one where network policy is actually
enforced. `deployment/scripts/verify-network-policies.sh` proves enforcement
with live connection probes rather than asserting it.

## Layout

```text
infrastructure/terraform/
  main.tf, variables.tf, outputs.tf, providers.tf, versions.tf
  modules/{networking,security,identity,kubernetes,compute,storage}/
  policies/free-tier/        # the Always Free compliance gate + its tests
  .checkov.yaml              # static-analysis config; every skip has a reason
kubernetes/
  namespaces/   security/   networking/   database/   ingestion/
deployment/
  scripts/      # provisioning, deployment, verification, recovery
  toolbox/      # minimal OCI CLI image for maintenance workloads
  manifests/    # arm64-digests.json: the deployed image digests
                # sbom/: CycloneDX SBOMs, one per published image
  security/     # cosign.pub: the image signing public key
```

## First-time provisioning

```bash
cd infrastructure/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in OCIDs and your /32
terraform init
terraform plan -out=plan.tfplan
terraform show -json plan.tfplan > plan.json

# Refuses anything not provably Always Free eligible.
python3 policies/free-tier/check_plan.py plan.json

terraform apply plan.tfplan
../../deployment/scripts/terraform-state.sh push
```

## Deploying the data platform

```bash
export KUBECONFIG=~/.kube/algot-oke.config
deployment/scripts/setup-kubeconfig.sh
deployment/scripts/install-calico.sh          # policy enforcement

# Pinned, checksum-verified trivy, syft and cosign.
eval "$(deployment/scripts/install-security-tools.sh --print-path)"

deployment/scripts/build-arm64-images.sh      # build, test, scan, SBOM, sign, push
deployment/scripts/deploy-data-platform.sh    # verifies signatures, then applies
deployment/scripts/verify-deployment.sh
deployment/scripts/verify-network-policies.sh
deployment/scripts/registry-gc.sh             # reclaim superseded manifests
deployment/scripts/check-costs.sh             # prove the bill is £0.00
```

### The image supply chain

`build-arm64-images.sh` takes one service at a time (or all three when given
no arguments) and will not publish an image that fails any step:

1. Build for `linux/arm64` and push by digest. The build context is checked
   to be free of the proprietary `Strategies` tree first.
2. Assert the manifest is `arm64` and declares a non-root `USER` — a root
   image is rejected at admission by the namespace's restricted Pod Security
   Standards, so it is cheaper to catch here.
3. Smoke test the image *pulled back from the registry by digest*, so the
   test covers the exact artifact the cluster will pull.
4. Scan with trivy. Any HIGH or CRITICAL CVE **that has a fix available**
   fails the build. Findings with no released fix are excluded, and the ones
   currently accepted are listed in `docs/ci-cd/ARCHITECTURE.md`. Where a fix
   exists but the pinned base image does not yet carry it, the package is
   pinned explicitly in each Dockerfile's `base` stage.
5. Generate a CycloneDX SBOM into `deployment/manifests/sbom/`.
6. Sign the digest with cosign and attach two attestations: the SBOM, and a
   provenance predicate recording the commit, builder and time. Both are then
   verified against `deployment/security/cosign.pub`.
7. Merge the digest into `arm64-digests.json`, leaving other services' entries
   untouched.

By default the build runs on the workstation's Docker host under ARM64 binfmt
emulation, creating no cluster or OCI resource. `BUILD_MODE=cluster` restores
the original in-cluster buildx builder for native compilation on the Ampere
nodes, and removes its namespace afterwards.

The OCIR auth token and the cosign signing key are both read from OCI Vault
into a mode-700 temporary directory and shredded on exit. No registry
credential or private key is written to the operator's Docker config or
persisted anywhere. Signing uses a Vault-held key rather than a keyless
Sigstore identity, so the one committed public key verifies every image
whether it was built locally or by the ARM64 workflow.

`deploy-data-platform.sh` re-verifies each digest's signature before applying
anything and refuses to deploy an unsigned image.

### Registry storage

Every rebuild leaves the previous manifest untagged but still stored and
still billable, and nothing in OCIR expires it. `registry-gc.sh` reports
usage and reclaims those orphans; it is read-only until given `--apply`.

A manifest is only ever a deletion candidate when it is untagged, absent from
`arm64-digests.json`, and not in use by a running pod — so the image a pod is
still running is never removed, even after its tag has moved on. Cosign
signature and attestation artifacts are retained for as long as their subject
image is, and removed once it is gone. The script fails if total storage
exceeds `STORAGE_BUDGET_GB` (5 GB by default), and the ARM64 workflow applies
the same budget after every build.

## Running a backfill

```bash
# As a Job (restores and re-uploads the coverage store):
deployment/scripts/run-backfill.sh --symbol AAPL --provider alpaca

# Against the live pod's own coverage store:
kubectl exec -n algot-data deployment/data-ingestion -c live-ingestion -- \
  market-backfill --symbol AAPL
```

## Drift detection

`terraform plan` only answers one of the three questions that matter, so
[`drift-detect.sh`](../../deployment/scripts/drift-detect.sh) asks all three.
It is read-only and takes no state lock, so it is safe to run at any time.

```bash
deployment/scripts/drift-detect.sh            # all three checks
deployment/scripts/drift-detect.sh plan       # config vs reality
deployment/scripts/drift-detect.sh unmanaged  # reality vs state
deployment/scripts/drift-detect.sh costs      # the bill
```

| Check | Question | What only it catches |
| --- | --- | --- |
| `plan` | Does live infrastructure match the configuration? | A resource edited in the console; a committed change never applied |
| `unmanaged` | Does anything exist that is in no state file? | A resource created by hand — a plan cannot drift on a resource Terraform has never heard of |
| `costs` | Is the bill still £0.00? | A free allowance exceeded, or a paid resource outside this compartment |

Exit codes are `0` clean, `1` the check could not run, `2` drift found.
The `plan` check also re-runs the free-tier gate against the current planned
state, so an allowance that has been outgrown shows up as a failure rather
than as a surprise on the next apply.

`unmanaged` reports a resource that is already `PENDING_DELETION` as a note
rather than a finding: there is nothing to import and nothing to do.

[`infrastructure-drift.yml`](../../.github/workflows/infrastructure-drift.yml)
runs all three daily at 06:12 UTC, and on a finding opens — or comments on —
a single `Infrastructure drift detected` issue rather than filing a new one
every day.

## Further reading

- [FREE-TIER.md](FREE-TIER.md) — the allowance accounting and how cost is verified
- [STATE.md](STATE.md) — where Terraform state lives, how it is locked and how to recover it
- [RUNBOOK.md](RUNBOOK.md) — recovery procedures
- [ACCESS.md](ACCESS.md) — cluster access, state management, CI credentials
