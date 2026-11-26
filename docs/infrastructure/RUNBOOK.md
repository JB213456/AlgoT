# Recovery runbook

Procedures for the failure modes this platform can hit. Each one states what
is lost, what is recoverable, and the exact commands.

Set `export KUBECONFIG=~/.kube/algot-oke.config` first.

## What is durable, and what is not

| Data | Lives on | Survives |
|---|---|---|
| Market data (parquet + metadata) | 80 GB OCI block volume, `Retain` | pod deletion, namespace deletion, node loss, cluster rebuild |
| Database backups | same volume (`/data/backups`) + `algot-backups` bucket | volume loss (bucket copy) |
| Backfill coverage store | `emptyDir` + Object Storage snapshots | pod and node loss (restored on start) |
| Live in-flight events | in-memory queue, flushed every 5 s | nothing — up to 5 s may be lost on hard kill |
| Terraform state | local file + versioned bucket | workstation loss |

The database volume is protected three ways: `prevent_destroy` in Terraform,
`persistentVolumeReclaimPolicy: Retain` on the PV, and a hard rejection in
the free-tier gate for any plan that would destroy or replace it.

## Database pod failure

The StatefulSet restarts it automatically. Confirm recovery:

```bash
kubectl -n algot-data get pod -l app.kubernetes.io/name=market-data-db
kubectl -n algot-data logs -l app.kubernetes.io/name=market-data-db --tail=50
```

If it will not start, the usual causes are the volume failing to attach or
the auth secret missing:

```bash
kubectl -n algot-data describe pod -l app.kubernetes.io/name=market-data-db | tail -30
kubectl -n algot-data get pvc,pv
kubectl -n algot-data get secret market-data-db-auth
```

## Worker node failure

With both nodes in one AD, the database volume can attach to the survivor, so
the pod reschedules on its own. If the node is stuck `NotReady`:

```bash
kubectl get nodes
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data --force
```

Replacing a node through OCI recycles it into the pool at the same size — it
does not change the allocation, so it stays inside the allowance:

```bash
oci ce node-pool get --node-pool-id "$(terraform -chdir=infrastructure/terraform output -raw node_pool_id)"
```

Do **not** raise the pool size to compensate. 2 × 2 OCPU is the entire
Always Free allowance; a third node bills immediately.

## Database restore

Backups are tar.gz archives created by the database's own backup machinery,
kept on the volume (newest 2) and in `algot-backups` (30 days).

```bash
# List what is available on the volume
kubectl -n algot-data exec deployment/market-data-db -- \
  curl -fsS -H "X-API-Key: $KEY" http://127.0.0.1:8000/v1/backups

# Restore a named archive
kubectl -n algot-data exec deployment/market-data-db -- \
  curl -fsS -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"archive":"<name>.tar.gz","overwrite":true}' \
  http://127.0.0.1:8000/v1/backups/restore
```

If the archive exists only in Object Storage, copy it back onto the volume
first — the backup CronJob pod mounts the same volume:

```bash
oci os object get --namespace "$NS" --bucket-name algot-backups \
  --name "database/<name>.tar.gz" --file /tmp/<name>.tar.gz
kubectl -n algot-data cp /tmp/<name>.tar.gz \
  "$(kubectl -n algot-data get pod -l app.kubernetes.io/name=market-data-db \
     -o jsonpath='{.items[0].metadata.name}')":/data/backups/
```

Run `deployment/scripts/verify-deployment.sh` afterwards.

## Interrupted ingestion

Live ingestion reconnects on its own; `live_runner` traps `SIGTERM`, flushes
buffered events and exits cleanly, and the liveness probe restarts the pod if
the flush loop wedges.

```bash
kubectl -n algot-data logs deployment/data-ingestion -c live-ingestion --tail=100
kubectl -n algot-data exec deployment/data-ingestion -c live-ingestion -- \
  python3 -m ingestion_runtime.healthcheck
```

A stale heartbeat with a healthy process means the async loop is stuck —
restart it:

```bash
kubectl -n algot-data rollout restart deployment/data-ingestion
```

An interrupted **backfill** resumes from its coverage store, which the init
container restores from Object Storage. Re-run the same command; already
downloaded ranges are skipped. Check coverage first:

```bash
deployment/scripts/run-backfill.sh --symbol AAPL --coverage
```

## Persistent volume recovery

If the PVC is deleted, the PV stays `Released` and the OCI volume keeps the
data. Re-bind it:

```bash
# Clear the stale claim reference, then re-apply the PVC
kubectl patch pv market-data-db-pv --type=json \
  -p='[{"op":"remove","path":"/spec/claimRef"}]'
deployment/scripts/deploy-data-platform.sh
```

If the PV itself is gone, recreate it from the Terraform output — the volume
is untouched:

```bash
terraform -chdir=infrastructure/terraform output -raw database_volume_id
```

## Terraform state recovery

State is synced to a versioned bucket.

```bash
deployment/scripts/terraform-state.sh versions
deployment/scripts/terraform-state.sh restore <version-id>
terraform -chdir=infrastructure/terraform plan    # must be empty
```

If state is lost entirely, re-import rather than re-create — re-creating
would fail on existing resources and risks the database volume:

```bash
terraform import module.storage.oci_core_volume.database <volume-ocid>
terraform import module.kubernetes.oci_containerengine_cluster.main <cluster-ocid>
terraform import module.storage.oci_objectstorage_bucket.backups n/<ns>/b/algot-backups
# ...then plan and confirm it is empty before applying anything
```

## Cluster reconstruction

The tenancy allows **one** cluster, so this is destroy-then-create. The
database volume is a separate resource and is not touched.

```bash
# 1. Confirm the volume will survive: it must NOT appear in the destroy list
terraform -chdir=infrastructure/terraform plan -out=plan.tfplan
terraform -chdir=infrastructure/terraform show -json plan.tfplan > plan.json
python3 infrastructure/terraform/policies/free-tier/check_plan.py plan.json
#    -> rejects cluster destruction unless you pass --allow-cluster-replacement

# 2. Apply deliberately
python3 .../check_plan.py plan.json --allow-cluster-replacement
terraform -chdir=infrastructure/terraform apply plan.tfplan

# 3. Rebuild access and redeploy; the volume re-attaches with its data
deployment/scripts/setup-kubeconfig.sh
deployment/scripts/install-calico.sh
deployment/scripts/deploy-data-platform.sh
deployment/scripts/verify-deployment.sh
```

## Nodes stuck NotReady, NativePodNetwork in BACKOFF

Symptom:

```
kubectl get nodes          # both NotReady
kubectl get npn            # STATE=BACKOFF  REASON=FailedToGetInstance
kubectl -n kube-system get pods | grep vcn-native   # Init:0/2
```

The node's kubelet reports `no CNI configuration file in /etc/cni/net.d/`,
and the CNI init container logs `Detected non-success status {state: BACKOFF}`.

**Cause found on 24 November 2026.** The service gateway, private route,
and API NSG egress selected the Object Storage-only service CIDR from
`data.oci_core_services.all.services[0]`. The CNI then timed out connecting
to `iaas.uk-london-1.oraclecloud.com:443`. `FailedToGetInstance` did not
establish an IAM denial. Selecting the `All ... Services in Oracle Services
Network` service in Terraform fixed the route and both current nodes became
Ready with successful secondary VNIC and pod IP attachment.

**Check both the work request and CNI logs.** The node pool work request
reported `PodNetworkTimeOut`, a generic symptom. The CNI logs contained the
decisive TCP timeout. Check the service gateway destination and private
route before changing IAM:

```bash
C="$(terraform -chdir=infrastructure/terraform output -raw compartment_ocid)"
oci ce work-request list -c "$C" --all \
  --query 'data[?status==`FAILED`].{op:"operation-type",t:"time-accepted",id:id}' --output table
oci ce work-request-error list -c "$C" --work-request-id <id> \
  --query 'data[].{code:code,msg:message}' --output table
```

For this failure it returns:

```
PodNetworkTimeOut   1 nodes(s) pod network configuration timeout. If cluster's
                    related resources such as node pools, VCN and VCN resources
                    are in a different compartment to the cluster itself, confirm
                    that additional IAM policies are in place.
```

That generic message mentioned cross-compartment IAM even though the
resources were in one compartment and OCI Audit recorded successful CNI API
calls by the cluster and nodepool principals.

**Confirm the pod VNIC is the thing missing.** A working node has two VNIC
attachments (primary in the worker subnet, secondary in the pod subnet); a
blocked node has only one:

```bash
oci compute vnic-attachment list -c "$C" --instance-id <node-ocid> \
  --query 'data[].{nic:"nic-index",state:"lifecycle-state",subnet:"subnet-id"}' --output table
```

**Check the narrowly scoped cluster policy:**

```bash
oci iam policy list -c "$C" \
  --all --query 'data[?contains(name,`cluster-principal`)].statements' --output json
```

`modules/identity_cluster` manages compartment-specific grants conditioned
on the exact cluster and nodepool principal OCIDs. OCI Audit on 24 November
showed successful `GetInstance`, `CreatePrivateIp`, and `AttachVnic.begin`
calls. Do not broaden IAM or recycle workers on the strength of
`FailedToGetInstance` alone.

**Stale NPN entries are harmless.** `kubectl get npn` keeps an entry per
instance OCID, including for nodes that have since been deleted. Those will
sit in BACKOFF forever and can be ignored; match the entry name against the
live instance OCIDs before concluding anything:

```bash
NP="$(terraform -chdir=infrastructure/terraform output -raw node_pool_id)"
oci ce node-pool get --node-pool-id "$NP" --query 'data.nodes[].id'
```

**Require the current nodes' NPNs to reach SUCCESS.** `IN_PROGRESS` alone
does not establish that pod networking works. Also test a scheduled pod,
DNS, service access, and NetworkPolicy enforcement.

**Also verify the pod-CIDR security rules exist**, which Oracle requires and
which are easy to miss: the API endpoint NSG must allow ingress from the pod
CIDR on 6443 and 12250, and egress to it. See `modules/security`.

## Locked out of the Kubernetes API

The endpoint only accepts the CIDRs in `api_allowed_cidrs`. If your address
changed:

```bash
curl -s https://checkip.amazonaws.com
# set api_allowed_cidrs = ["<address>/32"] in terraform.tfvars
terraform -chdir=infrastructure/terraform apply   # via plan + gate
```

This only adds an NSG rule; it does not touch the cluster or the data.

## Rolling back a deployment

Images are pinned by digest, so rollback is exact:

```bash
kubectl -n algot-data rollout undo statefulset/market-data-db
kubectl -n algot-data rollout undo deployment/data-ingestion
kubectl -n algot-data rollout status statefulset/market-data-db
```

Either service can be rolled back without the other.
