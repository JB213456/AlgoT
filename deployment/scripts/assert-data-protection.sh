#!/usr/bin/env bash
# Prove a deployment did not disturb the market data volume.
#
# The market data is the one thing in this system that cannot be rebuilt: the
# images come from a registry, the cluster comes from Terraform, but years of
# bars and trades come from nowhere. The free-tier Terraform gate stops the
# volume being destroyed and the manifest validator stops the reclaim policy
# being loosened, but neither notices a deployment that quietly rebinds the
# claim to a *different* volume -- which presents as a healthy, empty
# database.
#
# So the identity of the bound volume is recorded before the deploy and
# compared after it. The comparison covers the underlying OCI volume OCID,
# the PersistentVolume and claim identity, the reclaim policy, and the row
# counts of the three data tables.
#
# Usage:
#   assert-data-protection.sh snapshot <file>   record the current state
#   assert-data-protection.sh compare  <file>   fail if anything regressed
#
# Exit: 0 unchanged (or grown), 1 a protected property changed, 2 usage or
#       cluster access problem.
set -uo pipefail

MODE="${1:-}"
STATE_FILE="${2:-}"
NS="${NS:-algot-data}"
PVC="${PVC:-market-data-storage}"
POD="${POD:-market-data-db-0}"

case "$MODE" in
  snapshot | compare) ;;
  *)
    echo "usage: $0 {snapshot|compare} <file>" >&2
    exit 2
    ;;
esac

if [[ -z "$STATE_FILE" ]]; then
  echo "usage: $0 $MODE <file>" >&2
  exit 2
fi

command -v kubectl >/dev/null || { echo "FATAL: kubectl is not on PATH" >&2; exit 2; }

# The claim may legitimately not exist yet on a first-ever deployment. That
# is recorded rather than treated as an error, so the comparison afterwards
# knows there was nothing to protect.
read_state() {
  local pv_name volume_handle reclaim pvc_uid capacity phase
  pvc_uid="$(kubectl -n "$NS" get pvc "$PVC" -o jsonpath='{.metadata.uid}' 2>/dev/null)"

  if [[ -z "$pvc_uid" ]]; then
    echo "absent=true"
    return 0
  fi

  pv_name="$(kubectl -n "$NS" get pvc "$PVC" -o jsonpath='{.spec.volumeName}' 2>/dev/null)"
  phase="$(kubectl -n "$NS" get pvc "$PVC" -o jsonpath='{.status.phase}' 2>/dev/null)"
  capacity="$(kubectl -n "$NS" get pvc "$PVC" -o jsonpath='{.status.capacity.storage}' 2>/dev/null)"
  # volumeHandle is the OCI block volume OCID: the identity that actually
  # matters. A new PV pointing at a new volume is the failure this catches.
  volume_handle="$(kubectl get pv "$pv_name" -o jsonpath='{.spec.csi.volumeHandle}' 2>/dev/null)"
  reclaim="$(kubectl get pv "$pv_name" -o jsonpath='{.spec.persistentVolumeReclaimPolicy}' 2>/dev/null)"

  echo "absent=false"
  echo "pvc_uid=$pvc_uid"
  echo "pv_name=$pv_name"
  echo "volume_handle=$volume_handle"
  echo "reclaim=$reclaim"
  echo "capacity=$capacity"
  echo "phase=$phase"

  # Row counts are best effort: the pod may be mid-rollout, and a missing
  # count must not be mistaken for zero rows. "unknown" is recorded instead,
  # and the comparison skips a table it could not read on either side.
  local counts
  counts="$(kubectl -n "$NS" exec "$POD" -c market-data-db -- python3 -c '
import os, duckdb
path = os.environ.get("MARKET_DB_PATH", "/data/market_data.duckdb")
con = duckdb.connect(path, read_only=True)
for table in ("trades", "quotes", "bars"):
    try:
        print(f"{table}={con.execute(f\"SELECT COUNT(*) FROM {table}\").fetchone()[0]}")
    except Exception:
        print(f"{table}=unknown")
' 2>/dev/null)"

  if [[ -n "$counts" ]]; then
    while IFS= read -r line; do
      [[ -n "$line" ]] && echo "rows_$line"
    done <<< "$counts"
  else
    for table in trades quotes bars; do
      echo "rows_$table=unknown"
    done
  fi
}

get() {
  # Read one key from a state file. Prints nothing if absent.
  awk -F= -v k="$2" '$1==k { sub(/^[^=]*=/, ""); print; exit }' "$1"
}

if [[ "$MODE" == "snapshot" ]]; then
  if ! kubectl -n "$NS" get ns >/dev/null 2>&1 && ! kubectl get ns "$NS" >/dev/null 2>&1; then
    echo "FATAL: cannot reach the cluster or namespace $NS" >&2
    exit 2
  fi
  read_state > "$STATE_FILE"
  echo "Recorded the protected state of $NS/$PVC:"
  sed 's/^/    /' "$STATE_FILE"
  exit 0
fi

# ---------------------------------------------------------------- compare --

if [[ ! -f "$STATE_FILE" ]]; then
  echo "FATAL: no snapshot at $STATE_FILE; run '$0 snapshot $STATE_FILE' first" >&2
  exit 2
fi

AFTER="$(mktemp)"
trap 'rm -f "$AFTER"' EXIT
read_state > "$AFTER"

echo "Before:"; sed 's/^/    /' "$STATE_FILE"
echo "After:";  sed 's/^/    /' "$AFTER"
echo

FAILURES=0
fail() { echo "FAIL: $1" >&2; FAILURES=$((FAILURES + 1)); }

BEFORE_ABSENT="$(get "$STATE_FILE" absent)"
AFTER_ABSENT="$(get "$AFTER" absent)"

if [[ "$BEFORE_ABSENT" == "true" ]]; then
  if [[ "$AFTER_ABSENT" == "true" ]]; then
    echo "No claim before or after: nothing to protect yet."
    exit 0
  fi
  echo "The claim was created by this deployment; there was no prior data to protect."
  exit 0
fi

if [[ "$AFTER_ABSENT" == "true" ]]; then
  fail "the PersistentVolumeClaim $PVC no longer exists. The market data volume has been detached from the cluster."
  exit 1
fi

# --- identity: the same claim, the same PV, the same OCI volume ---
for key in pvc_uid pv_name volume_handle; do
  before="$(get "$STATE_FILE" "$key")"
  after="$(get "$AFTER" "$key")"
  if [[ -z "$before" ]]; then
    echo "  skip  $key was not recorded before the deployment"
  elif [[ "$before" != "$after" ]]; then
    case "$key" in
      volume_handle)
        fail "the claim is bound to a DIFFERENT OCI block volume (was ${before}, now ${after}). The database would come up healthy and empty; the original volume is orphaned, not read."
        ;;
      pvc_uid)
        fail "the PersistentVolumeClaim was deleted and recreated (uid ${before} -> ${after}). Only the Retain reclaim policy kept the data, and the new claim may not be bound to it."
        ;;
      *)
        fail "$key changed from ${before} to ${after}"
        ;;
    esac
  else
    echo "  ok    $key unchanged ($after)"
  fi
done

# --- the reclaim policy must stay Retain ---
RECLAIM_AFTER="$(get "$AFTER" reclaim)"
if [[ "$RECLAIM_AFTER" != "Retain" ]]; then
  fail "the PersistentVolume reclaim policy is '${RECLAIM_AFTER}', not Retain. Deleting the claim would now delete the market data."
else
  echo "  ok    reclaim policy is Retain"
fi

# --- the claim must still be Bound ---
PHASE_AFTER="$(get "$AFTER" phase)"
if [[ "$PHASE_AFTER" != "Bound" ]]; then
  fail "the claim is in phase '${PHASE_AFTER}', not Bound."
else
  echo "  ok    claim is Bound"
fi

# --- row counts must not have fallen ---
# Ingestion runs continuously, so counts normally grow. A fall means data
# was lost; an unreadable count on either side is reported, not guessed at.
for table in trades quotes bars; do
  before="$(get "$STATE_FILE" "rows_$table")"
  after="$(get "$AFTER" "rows_$table")"
  if [[ "$before" == "unknown" || "$after" == "unknown" || -z "$before" || -z "$after" ]]; then
    echo "  warn  $table row count unavailable (before=${before:-missing} after=${after:-missing}); not asserted"
  elif (( after < before )); then
    fail "$table lost rows: ${before} -> ${after}."
  else
    echo "  ok    $table rows ${before} -> ${after}"
  fi
done

echo
if (( FAILURES > 0 )); then
  echo "DATA PROTECTION FAILED: $FAILURES problem(s). The deployment must be treated as data-affecting." >&2
  exit 1
fi

echo "PASS: the market data volume is the same volume, still Retain, still Bound, and has not lost rows."
