#!/usr/bin/env bash
# Integration and resilience verification for the DEPLOYED market data database.
#
# Everything here asserts an observable outcome against the running Kubernetes
# Service, not against a local copy of the library:
#
#   1. HTTP API suite, run from a client pod through the ClusterIP Service
#      (auth, write/read integrity, dedup, compaction, point-in-time,
#      concurrency, storage status, restore guards).
#   2. Backup and restore, with the live volume mounted read-only and the
#      restores landing in isolated scratch space.
#   3. The volume-expansion guard: a resize must be refused.
#   4. Graceful shutdown and container restart recovery.
#   5. Pod replacement: data must survive the pod being deleted outright.
#   6. The storage-utilisation monitor.
#
# Pre-existing market data is never written to or deleted. The suite's own
# synthetic rows are isolated behind provider="itest-<run id>" and removed at
# the end unless KEEP_TEST_DATA=1.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
K8S_DIR="$REPO_ROOT/kubernetes"
TESTS_DIR="$REPO_ROOT/Data/Database/tests/integration"
DIGESTS="${DIGEST_FILE:-$REPO_ROOT/deployment/manifests/arm64-digests.json}"
MANIFEST="$K8S_DIR/database/integration-tests.yaml"
NS=algot-data
STS=market-data-db
POD=market-data-db-0

RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
TEST_PROVIDER="itest-$RUN_ID"
KEEP_TEST_DATA="${KEEP_TEST_DATA:-0}"
KEEP_ARCHIVE="${KEEP_ARCHIVE:-0}"

PASS=0
FAIL=0
ok()   { printf '  PASS  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL+1)); }
note() { printf '        %s\n' "$1"; }
section() { printf '\n=== %s ===\n' "$1"; }

[[ -f "$DIGESTS" ]] || { echo "FATAL: no digest file at $DIGESTS" >&2; exit 1; }
DATABASE_IMAGE="$(python3 -c "import json;print(json.load(open('$DIGESTS'))['database'])")"
TOOLBOX_IMAGE="$(python3 -c "import json;print(json.load(open('$DIGESTS'))['toolbox'])")"

# Renders one document out of the multi-document manifest, substituting the
# __PLACEHOLDER__ values from the environment.
render_job() {
  local job="$1"
  python3 - "$MANIFEST" "$job" <<'PY'
import os, re, sys
path, job = sys.argv[1], sys.argv[2]
docs = re.split(r'(?m)^---$', open(path).read())
doc = next(d for d in docs if re.search(r'^\s*name:\s*%s\s*$' % re.escape(job), d, re.M))
def sub(m):
    key = m.group(1)
    if key not in os.environ:
        sys.exit("FATAL: placeholder __%s__ has no value" % key)
    return os.environ[key]
sys.stdout.write(re.sub(r'__([A-Z_]+)__', sub, doc))
PY
}

wait_job() {
  local job="$1" timeout="${2:-1500}" waited=0 succ fail
  while [[ "$waited" -lt "$timeout" ]]; do
    succ="$(kubectl -n "$NS" get job "$job" -o jsonpath='{.status.succeeded}' 2>/dev/null)"
    fail="$(kubectl -n "$NS" get job "$job" -o jsonpath='{.status.failed}' 2>/dev/null)"
    [[ "${succ:-0}" -ge 1 ]] && return 0
    [[ "${fail:-0}" -ge 1 ]] && return 1
    sleep 5; waited=$((waited+5))
  done
  echo "    (timed out after ${timeout}s)" >&2
  return 2
}

# Physical row count for one dataset, read through the API from inside the
# database pod. The key never leaves the pod: it is taken from the pod's own
# environment rather than from the Secret on this machine.
row_count() {
  local ds="$1"
  kubectl -n "$NS" exec "$POD" -- python3 -c "
import json, os, urllib.request
key = os.environ['MARKET_DATA_DB_API_KEYS'].split(',')[0]
req = urllib.request.Request('http://127.0.0.1:8000/v1/datasets/$ds/row-count?dedup=none',
                             headers={'X-API-Key': key})
print(json.load(urllib.request.urlopen(req, timeout=120))['row_count'])
" 2>/dev/null | tr -d '\r'
}

echo "AlgoT market data database -- integration and resilience verification"
echo "run id : $RUN_ID"
echo "target : $NS/$STS (Service market-data-db:8000)"
date -u +'started: %Y-%m-%dT%H:%M:%SZ'

# ----------------------------------------------------------------- preflight --
section "Preflight"
if ! kubectl -n "$NS" get statefulset "$STS" >/dev/null 2>&1; then
  echo "FATAL: statefulset/$STS is not deployed." >&2
  exit 1
fi
if kubectl -n "$NS" rollout status "statefulset/$STS" --timeout=300s >/dev/null 2>&1; then
  ok "database StatefulSet is rolled out and ready"
else
  bad "database StatefulSet is not ready"
  kubectl -n "$NS" get pods -l app.kubernetes.io/name="$STS"
  exit 1
fi

BASELINE_TRADES="$(row_count trades)"
BASELINE_QUOTES="$(row_count quotes)"
BASELINE_BARS="$(row_count bars)"
for v in BASELINE_TRADES BASELINE_QUOTES BASELINE_BARS; do
  [[ "${!v}" =~ ^[0-9]+$ ]] || { echo "FATAL: could not read $v" >&2; exit 1; }
done
note "baseline physical rows: trades=$BASELINE_TRADES quotes=$BASELINE_QUOTES bars=$BASELINE_BARS"
export BASELINE_TRADES BASELINE_QUOTES BASELINE_BARS RUN_ID DATABASE_IMAGE TOOLBOX_IMAGE

# ------------------------------------------------------- test scripts as a ConfigMap --
section "Publishing the test suite"
if kubectl -n "$NS" create configmap database-integration-tests \
     --from-file="$TESTS_DIR/live_service_tests.py" \
     --from-file="$TESTS_DIR/restore_isolation_test.py" \
     --dry-run=client -o yaml | kubectl apply -f - >/dev/null; then
  ok "ConfigMap database-integration-tests applied"
else
  bad "could not apply the test ConfigMap"; exit 1
fi

# -------------------------------------------------------------- 1. HTTP API suite --
section "1. HTTP API integration tests (through the ClusterIP Service)"
kubectl -n "$NS" delete job market-data-db-integration --ignore-not-found --wait=true >/dev/null 2>&1
render_job market-data-db-integration | kubectl apply -f - >/dev/null || { bad "could not create the API test Job"; exit 1; }

wait_job market-data-db-integration 1500
API_RC=$?
API_LOG="$(kubectl -n "$NS" logs job/market-data-db-integration --tail=-1 2>/dev/null)"
echo "$API_LOG" | sed 's/^/    /'
if [[ "$API_RC" -eq 0 ]]; then
  ok "API integration suite passed"
else
  bad "API integration suite failed (job rc=$API_RC)"
fi

ARCHIVE="$(printf '%s\n' "$API_LOG" | sed -n 's/^ARCHIVE=//p' | tail -1)"
SUITE_PROVIDER="$(printf '%s\n' "$API_LOG" | sed -n 's/^TEST_PROVIDER=//p' | tail -1)"
[[ -n "$SUITE_PROVIDER" ]] && TEST_PROVIDER="$SUITE_PROVIDER"
# 120 rows for the main symbol + 6 concurrent writers x 50 rows.
EXPECTED_PROVIDER_ROWS="${EXPECTED_PROVIDER_ROWS:-420}"
if [[ -n "$ARCHIVE" ]]; then
  note "backup archive produced by the suite: $ARCHIVE"
else
  bad "the suite did not report a backup archive; restore phase B will be skipped"
fi

# ------------------------------------------------- 2. backup / restore in isolation --
section "2. Backup and restore (isolated scratch root, live volume read-only)"
export ARCHIVE TEST_PROVIDER EXPECTED_PROVIDER_ROWS
kubectl -n "$NS" delete job market-data-db-restore-test --ignore-not-found --wait=true >/dev/null 2>&1
render_job market-data-db-restore-test | kubectl apply -f - >/dev/null || { bad "could not create the restore test Job"; exit 1; }

wait_job market-data-db-restore-test 1500
RESTORE_RC=$?
kubectl -n "$NS" logs job/market-data-db-restore-test --tail=-1 2>/dev/null | sed 's/^/    /'
if [[ "$RESTORE_RC" -eq 0 ]]; then
  ok "backup/restore suite passed"
else
  bad "backup/restore suite failed (job rc=$RESTORE_RC)"
fi

# --------------------------------------------------------- 3. volume expansion guard --
section "3. Uncontrolled volume expansion is prevented"
EXPAND="$(kubectl get storageclass algot-bv-retain -o jsonpath='{.allowVolumeExpansion}' 2>/dev/null)"
[[ "$EXPAND" == "false" ]] && ok "StorageClass algot-bv-retain sets allowVolumeExpansion=false" \
                           || bad "allowVolumeExpansion is '$EXPAND' (expected false)"

PROVISIONER="$(kubectl get storageclass algot-bv-retain -o jsonpath='{.provisioner}' 2>/dev/null)"
[[ "$PROVISIONER" == "kubernetes.io/no-provisioner" ]] \
  && ok "StorageClass cannot dynamically provision a second volume" \
  || bad "provisioner is '$PROVISIONER' (expected kubernetes.io/no-provisioner)"

# The decisive check: ask the API server to grow the claim and require refusal.
RESIZE_OUT="$(kubectl -n "$NS" patch pvc market-data-db-data \
  -p '{"spec":{"resources":{"requests":{"storage":"120Gi"}}}}' 2>&1)"
RESIZE_RC=$?
if [[ "$RESIZE_RC" -ne 0 ]]; then
  ok "a live request to grow the PVC to 120Gi was refused by the API server"
  note "$(printf '%s' "$RESIZE_OUT" | head -1)"
else
  bad "the PVC accepted a resize to 120Gi -- free-tier storage is not protected"
  kubectl -n "$NS" patch pvc market-data-db-data \
    -p '{"spec":{"resources":{"requests":{"storage":"80Gi"}}}}' >/dev/null 2>&1
fi

CUR_SIZE="$(kubectl -n "$NS" get pvc market-data-db-data -o jsonpath='{.spec.resources.requests.storage}' 2>/dev/null)"
[[ "$CUR_SIZE" == "80Gi" ]] && ok "claim is still 80Gi after the attempted resize" \
                            || bad "claim size is now '$CUR_SIZE'"

# -------------------------------------------- 4. graceful shutdown + container restart --
section "4. Graceful shutdown and container restart recovery"
PRE_RESTARTS="$(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
PRE_UID="$(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.metadata.uid}')"
BARS_BEFORE="$(row_count bars)"
note "restartCount before: $PRE_RESTARTS; bars physical rows: $BARS_BEFORE"

# SIGTERM to PID 1 is exactly what the kubelet sends on eviction, and PID 1 is
# uvicorn itself (the entrypoint execs it), so this exercises the real
# shutdown path rather than a simulated one.
kubectl -n "$NS" exec "$POD" -- sh -c 'kill -TERM 1' >/dev/null 2>&1

RESTARTED=0
for _ in $(seq 1 60); do
  NOW="$(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null)"
  if [[ "${NOW:-0}" -gt "${PRE_RESTARTS:-0}" ]]; then RESTARTED=1; break; fi
  sleep 3
done
[[ "$RESTARTED" -eq 1 ]] && ok "the container exited on SIGTERM and the kubelet restarted it" \
                         || bad "the container did not restart within 180s"

# The lifespan hook logs this line only when it flushes and closes the store
# cleanly, so its presence in the previous container's log is the proof of a
# graceful rather than killed shutdown.
PREV_LOG="$(kubectl -n "$NS" logs "$POD" --previous --tail=40 2>/dev/null)"
if printf '%s' "$PREV_LOG" | grep -q 'flushing and closing database'; then
  ok "the terminating container flushed and closed the database (graceful shutdown)"
else
  bad "no graceful-shutdown log line found in the previous container"
  printf '%s\n' "$PREV_LOG" | tail -10 | sed 's/^/        /'
fi

SAME_POD="$(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.metadata.uid}')"
[[ "$SAME_POD" == "$PRE_UID" ]] && ok "the pod itself was not replaced (in-place container restart)" \
                                || bad "the pod was replaced, not restarted"

if kubectl -n "$NS" wait --for=condition=Ready "pod/$POD" --timeout=300s >/dev/null 2>&1; then
  ok "probes report the restarted container Ready again"
else
  bad "the restarted container did not become Ready within 300s"
fi

BARS_AFTER_RESTART="$(row_count bars)"
[[ "$BARS_AFTER_RESTART" == "$BARS_BEFORE" ]] \
  && ok "all $BARS_BEFORE bars rows intact after the container restart" \
  || bad "bars rows changed across the restart: $BARS_BEFORE -> $BARS_AFTER_RESTART"

# ------------------------------------------------ 5. pod replacement and persistence --
section "5. Data survives pod replacement"
OLD_PV="$(kubectl -n "$NS" get pvc market-data-db-data -o jsonpath='{.spec.volumeName}')"
OLD_NODE="$(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.spec.nodeName}')"
OLD_UID="$(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.metadata.uid}')"
TRADES_BEFORE="$(row_count trades)"
QUOTES_BEFORE="$(row_count quotes)"
BARS_BEFORE="$(row_count bars)"
note "before deletion: pod uid ${OLD_UID:0:8} on $OLD_NODE; trades=$TRADES_BEFORE quotes=$QUOTES_BEFORE bars=$BARS_BEFORE"

# --wait=false plus an explicit uid poll, deliberately. The StatefulSet
# recreates the pod under the SAME name, so neither `delete --wait` nor
# `rollout status` distinguishes "old pod still terminating" from "replacement
# ready" -- both can return while the original object is still present, and
# reading .metadata.uid at that moment yields the OLD pod.
DEL_OUT="$(kubectl -n "$NS" delete pod "$POD" --wait=false 2>&1)"
DEL_RC=$?
[[ "$DEL_RC" -eq 0 ]] && ok "deletion of $POD accepted by the API server" \
                      || bad "deletion was refused: $DEL_OUT"

NEW_UID=""
for _ in $(seq 1 100); do
  CUR="$(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.metadata.uid}' 2>/dev/null)"
  if [[ -n "$CUR" && "$CUR" != "$OLD_UID" ]]; then NEW_UID="$CUR"; break; fi
  sleep 3
done
[[ -n "$NEW_UID" ]] \
  && ok "the pod really was replaced (uid ${OLD_UID:0:8} -> ${NEW_UID:0:8})" \
  || bad "no replacement pod with a new uid appeared within 300s"

if kubectl -n "$NS" wait --for=condition=Ready "pod/$POD" --timeout=600s >/dev/null 2>&1; then
  ok "the replacement pod re-attached the volume and reached Ready"
else
  bad "the replacement pod did not become Ready within 600s"
  kubectl -n "$NS" describe pod "$POD" | sed -n '/^Events:/,$p' | tail -15
fi

NEW_PV="$(kubectl -n "$NS" get pvc market-data-db-data -o jsonpath='{.spec.volumeName}')"
[[ "$NEW_PV" == "$OLD_PV" ]] && ok "the replacement pod re-bound the same PersistentVolume ($NEW_PV)" \
                             || bad "the claim moved from '$OLD_PV' to '$NEW_PV'"

NEW_RESTARTS="$(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
if [[ -n "$NEW_UID" && "${NEW_RESTARTS:-1}" -eq 0 ]]; then
  ok "the replacement container started cleanly on the first attempt"
else
  note "replacement container restartCount is ${NEW_RESTARTS:-unknown}"
fi

TRADES_AFTER="$(row_count trades)"
QUOTES_AFTER="$(row_count quotes)"
BARS_AFTER="$(row_count bars)"
for ds in trades quotes bars; do
  case "$ds" in
    trades) b="$TRADES_BEFORE"; a="$TRADES_AFTER" ;;
    quotes) b="$QUOTES_BEFORE"; a="$QUOTES_AFTER" ;;
    bars)   b="$BARS_BEFORE";   a="$BARS_AFTER" ;;
  esac
  # Live ingestion may add rows while the test runs, so the assertion is that
  # nothing was lost, not that the count is frozen.
  if [[ "$a" -ge "$b" ]]; then
    ok "$ds: all $b rows survived pod replacement ($a rows now)"
  else
    bad "$ds: rows LOST across pod replacement: $b -> $a"
  fi
done

# The suite's own rows are a known quantity written through the Service before
# the pod was destroyed, so finding them again proves durability precisely.
SURVIVED="$(kubectl -n "$NS" exec "$POD" -- python3 -c "
import json, os, urllib.request
key = os.environ['MARKET_DATA_DB_API_KEYS'].split(',')[0]
body = json.dumps({'providers': ['$TEST_PROVIDER'], 'dedup': 'canonical'}).encode()
req = urllib.request.Request('http://127.0.0.1:8000/v1/datasets/bars/read', data=body,
                             method='POST',
                             headers={'X-API-Key': key, 'Content-Type': 'application/json'})
print(len(json.load(urllib.request.urlopen(req, timeout=180))['rows']))
" 2>/dev/null | tr -d '\r')"
[[ "$SURVIVED" == "$EXPECTED_PROVIDER_ROWS" ]] \
  && ok "all $EXPECTED_PROVIDER_ROWS rows written through the Service are readable after pod replacement" \
  || bad "expected $EXPECTED_PROVIDER_ROWS test rows after replacement, found '$SURVIVED'"

# ------------------------------------------------------ 6. storage utilisation monitor --
section "6. Storage utilisation monitoring"
if kubectl -n "$NS" get cronjob algot-data-monitor >/dev/null 2>&1; then
  ok "cronjob/algot-data-monitor is deployed ($(kubectl -n "$NS" get cronjob algot-data-monitor -o jsonpath='{.spec.schedule}'))"
  MON_JOB="monitor-check-$RUN_ID"
  MON_JOB="$(printf '%s' "$MON_JOB" | tr 'A-Z' 'a-z' | cut -c1-52)"
  kubectl -n "$NS" delete job "$MON_JOB" --ignore-not-found --wait=true >/dev/null 2>&1
  if kubectl -n "$NS" create job "$MON_JOB" --from=cronjob/algot-data-monitor >/dev/null 2>&1; then
    wait_job "$MON_JOB" 420
    MON_RC=$?
    MON_LOG="$(kubectl -n "$NS" logs "job/$MON_JOB" --tail=-1 2>/dev/null)"
    printf '%s\n' "$MON_LOG" | sed 's/^/    /'
    [[ "$MON_RC" -eq 0 ]] && ok "the monitor job completed successfully" \
                          || bad "the monitor job failed (rc=$MON_RC)"
    printf '%s' "$MON_LOG" | grep -q '^storage: .* used of ' \
      && ok "the monitor reports volume utilisation" \
      || bad "the monitor did not report storage utilisation"
    printf '%s' "$MON_LOG" | grep -q 'database: reachable' \
      && ok "the monitor confirms database reachability" \
      || bad "the monitor could not reach the database"
    kubectl -n "$NS" delete job "$MON_JOB" --ignore-not-found --wait=false >/dev/null 2>&1
  else
    bad "could not create a one-off job from cronjob/algot-data-monitor"
  fi
else
  bad "cronjob/algot-data-monitor is not deployed (storage utilisation is unmonitored)"
fi

# ------------------------------------------------------------------------- cleanup --
section "Cleanup"
if [[ "$KEEP_ARCHIVE" != "1" && -n "$ARCHIVE" ]]; then
  if kubectl -n "$NS" exec "$POD" -- sh -c "rm -f /data/backups/'$ARCHIVE'" >/dev/null 2>&1; then
    ok "removed the test backup archive from the volume ($ARCHIVE)"
  else
    note "could not remove $ARCHIVE; it remains under /data/backups"
  fi
else
  note "test backup archive retained: $ARCHIVE"
fi

if [[ "$KEEP_TEST_DATA" != "1" ]]; then
  # Safe because provider is the top partition level below the dataset: only
  # this run's synthetic rows live under provider=itest-<run id>.
  REMOVED="$(kubectl -n "$NS" exec "$POD" -- python3 -c "
import shutil
from pathlib import Path
removed = 0
for zone in ('landing', 'canonical', 'archive'):
    for d in Path('/data', zone).glob('dataset=*/provider=$TEST_PROVIDER'):
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
print(removed)
" 2>/dev/null | tr -d '\r')"
  if [[ "${REMOVED:-0}" -gt 0 ]]; then
    ok "removed this run's synthetic partitions from the lake ($REMOVED partition trees)"
    note "metadata rows for provider=$TEST_PROVIDER remain, by design, as an audit trail"
  else
    note "no synthetic partitions found to remove"
  fi
else
  note "synthetic test data retained under provider=$TEST_PROVIDER"
fi

kubectl -n "$NS" delete job market-data-db-integration market-data-db-restore-test \
  --ignore-not-found --wait=false >/dev/null 2>&1
kubectl -n "$NS" delete configmap database-integration-tests --ignore-not-found >/dev/null 2>&1

FINAL_TRADES="$(row_count trades)"
FINAL_QUOTES="$(row_count quotes)"
FINAL_BARS="$(row_count bars)"
section "Final state"
note "physical rows now: trades=$FINAL_TRADES quotes=$FINAL_QUOTES bars=$FINAL_BARS"
note "baseline at start: trades=$BASELINE_TRADES quotes=$BASELINE_QUOTES bars=$BASELINE_BARS"
for pair in "trades $BASELINE_TRADES $FINAL_TRADES" "quotes $BASELINE_QUOTES $FINAL_QUOTES" "bars $BASELINE_BARS $FINAL_BARS"; do
  set -- $pair
  if [[ "$3" -ge "$2" ]]; then
    ok "$1: no pre-existing rows lost by this test run"
  else
    bad "$1: row count fell from $2 to $3"
  fi
done
kubectl -n "$NS" exec "$POD" -- df -Ph /data | sed 's/^/        /'

printf '\n=== %d passed, %d failed ===\n' "$PASS" "$FAIL"
date -u +'finished: %Y-%m-%dT%H:%M:%SZ'
[[ "$FAIL" -eq 0 ]] || exit 1
