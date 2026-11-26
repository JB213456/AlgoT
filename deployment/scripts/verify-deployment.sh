#!/usr/bin/env bash
# End-to-end verification of the deployed algot-data platform.
#
# Each check asserts an observable outcome. Exits non-zero if any fails, so
# it can gate a CI deployment.
set -uo pipefail

NS=algot-data
PASS=0
FAIL=0
WARN=0

ok()   { printf '  PASS  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL+1)); }
warn() { printf '  WARN  %s\n' "$1"; WARN=$((WARN+1)); }

section() { printf '\n== %s ==\n' "$1"; }

db_pod() { kubectl -n "$NS" get pod -l app.kubernetes.io/name=market-data-db -o jsonpath='{.items[0].metadata.name}' 2>/dev/null; }
ing_pod() { kubectl -n "$NS" get pod -l app.kubernetes.io/name=data-ingestion -o jsonpath='{.items[0].metadata.name}' 2>/dev/null; }

echo "AlgoT data platform verification -- $(date -u +%Y-%m-%dT%H:%M:%SZ)"

section "Cluster and nodes"
NODES_READY=$(kubectl get nodes --no-headers 2>/dev/null | awk '$2=="Ready"' | wc -l | tr -d ' ')
NODES_TOTAL=$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')
[[ "$NODES_READY" == "2" ]] && ok "2 worker nodes Ready" || bad "$NODES_READY/$NODES_TOTAL nodes Ready (expected 2)"

ARM_NODES=$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.nodeInfo.architecture}{"\n"}{end}' 2>/dev/null | grep -c arm64)
[[ "$ARM_NODES" == "2" ]] && ok "both nodes are arm64" || bad "$ARM_NODES nodes report arm64 (expected 2)"

# Allocation must match the Always Free envelope exactly.
TOTAL_CPU=$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.capacity.cpu}{"\n"}{end}' | paste -sd+ | bc 2>/dev/null)
[[ "$TOTAL_CPU" == "4" ]] && ok "4 OCPUs total (Always Free ceiling)" || warn "node capacity reports ${TOTAL_CPU} CPUs"

section "Policy engine"
if kubectl -n kube-system get daemonset calico-node >/dev/null 2>&1; then
  R=$(kubectl -n kube-system get ds calico-node -o jsonpath='{.status.numberReady}')
  D=$(kubectl -n kube-system get ds calico-node -o jsonpath='{.status.desiredNumberScheduled}')
  [[ "$R" == "$D" && "$R" != "0" ]] && ok "calico-node ready on $R/$D nodes" || bad "calico-node ready on $R/$D nodes"
else
  bad "calico-node absent -- NetworkPolicy is NOT enforced"
fi
NP=$(kubectl -n "$NS" get networkpolicy --no-headers 2>/dev/null | wc -l | tr -d ' ')
[[ "$NP" -ge 6 ]] && ok "$NP NetworkPolicies present" || bad "only $NP NetworkPolicies present"

section "Namespace isolation and limits"
ENFORCE=$(kubectl get ns "$NS" -o jsonpath='{.metadata.labels.pod-security\.kubernetes\.io/enforce}' 2>/dev/null)
[[ "$ENFORCE" == "restricted" ]] && ok "Pod Security Standards enforced: restricted" || bad "PSS enforce is '$ENFORCE'"

LB_QUOTA=$(kubectl -n "$NS" get resourcequota algot-data-quota -o jsonpath='{.spec.hard.services\.loadbalancers}' 2>/dev/null)
[[ "$LB_QUOTA" == "0" ]] && ok "load balancers forbidden by quota (no billable Service possible)" || bad "loadbalancer quota is '$LB_QUOTA'"

if kubectl -n "$NS" get svc -o jsonpath='{.items[*].spec.type}' 2>/dev/null | grep -qE 'LoadBalancer|NodePort'; then
  bad "a billable Service type exists"
else
  ok "all Services are ClusterIP (database not publicly reachable)"
fi

section "Persistent storage"
PVC_PHASE=$(kubectl -n "$NS" get pvc market-data-db-data -o jsonpath='{.status.phase}' 2>/dev/null)
[[ "$PVC_PHASE" == "Bound" ]] && ok "database PVC Bound" || bad "database PVC phase is '$PVC_PHASE'"

RECLAIM=$(kubectl get pv market-data-db-pv -o jsonpath='{.spec.persistentVolumeReclaimPolicy}' 2>/dev/null)
[[ "$RECLAIM" == "Retain" ]] && ok "PV reclaim policy is Retain (market data protected)" || bad "PV reclaim policy is '$RECLAIM'"

CAP=$(kubectl get pv market-data-db-pv -o jsonpath='{.spec.capacity.storage}' 2>/dev/null)
[[ "$CAP" == "80Gi" ]] && ok "database volume is 80Gi" || warn "database volume capacity is '$CAP'"

section "Database"
DB=$(db_pod)
if [[ -z "$DB" ]]; then
  bad "no database pod found"
else
  PHASE=$(kubectl -n "$NS" get pod "$DB" -o jsonpath='{.status.phase}')
  [[ "$PHASE" == "Running" ]] && ok "database pod Running ($DB)" || bad "database pod phase '$PHASE'"

  READY=$(kubectl -n "$NS" get pod "$DB" -o jsonpath='{.status.containerStatuses[0].ready}')
  [[ "$READY" == "true" ]] && ok "database readiness probe passing" || bad "database not ready"

  RESTARTS=$(kubectl -n "$NS" get pod "$DB" -o jsonpath='{.status.containerStatuses[0].restartCount}')
  [[ "${RESTARTS:-0}" -le 2 ]] && ok "database restart count $RESTARTS" || warn "database has restarted $RESTARTS times"

  HEALTH=$(kubectl -n "$NS" exec "$DB" -- python3 -c "
import urllib.request, json
print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=10).read().decode())
" 2>/dev/null)
  if echo "$HEALTH" | grep -q '"status": *"ok"'; then
    ok "database /health reports ok"
  else
    bad "database /health did not report ok: ${HEALTH:-no response}"
  fi

  # Non-root execution is a hard requirement.
  UID_RUN=$(kubectl -n "$NS" exec "$DB" -- id -u 2>/dev/null | tr -d '\r')
  [[ "$UID_RUN" == "10001" ]] && ok "database runs as non-root uid 10001" || bad "database runs as uid '$UID_RUN'"
fi

section "Ingestion"
ING=$(ing_pod)
if [[ -z "$ING" ]]; then
  bad "no ingestion pod found"
else
  PHASE=$(kubectl -n "$NS" get pod "$ING" -o jsonpath='{.status.phase}')
  [[ "$PHASE" == "Running" ]] && ok "ingestion pod Running ($ING)" || bad "ingestion pod phase '$PHASE'"

  UID_RUN=$(kubectl -n "$NS" exec "$ING" -c live-ingestion -- id -u 2>/dev/null | tr -d '\r')
  [[ "$UID_RUN" == "10002" ]] && ok "ingestion runs as non-root uid 10002" || bad "ingestion runs as uid '$UID_RUN'"

  # The heartbeat is written by the flush loop, so a fresh heartbeat proves
  # the engine is running, not merely that the process exists.
  if kubectl -n "$NS" exec "$ING" -c live-ingestion -- python3 -m ingestion_runtime.healthcheck >/dev/null 2>&1; then
    ok "ingestion heartbeat is fresh (live engine running)"
  else
    bad "ingestion heartbeat is stale or missing"
  fi

  # Database connectivity from the ingestion pod, through the ClusterIP
  # Service and the NetworkPolicy that permits it.
  CONN=$(kubectl -n "$NS" exec "$ING" -c live-ingestion -- python3 -c "
import urllib.request
try:
    r = urllib.request.urlopen('http://market-data-db.algot-data.svc.cluster.local:8000/health', timeout=10)
    print('ok' if r.status == 200 else 'status %s' % r.status)
except Exception as e:
    print('error: %s' % e)
" 2>/dev/null)
  [[ "$CONN" == "ok" ]] && ok "ingestion reaches the database over ClusterIP DNS" || bad "ingestion -> database failed: $CONN"
fi

section "Scheduled maintenance"
# algot-strategies, algot-core and algot-monitoring each add their own
# CronJobs to this check as they are deployed.
for cj in market-data-db-backup market-data-db-compaction algot-data-monitor; do
  if kubectl -n "$NS" get cronjob "$cj" >/dev/null 2>&1; then
    SCHED=$(kubectl -n "$NS" get cronjob "$cj" -o jsonpath='{.spec.schedule}')
    ok "cronjob/$cj present ($SCHED)"
  else
    bad "cronjob/$cj missing"
  fi
done

section "Resource consumption"
kubectl -n "$NS" get resourcequota algot-data-quota \
  -o custom-columns='RESOURCE:.spec.hard,USED:.status.used' --no-headers 2>/dev/null \
  | head -2 || true
echo "  -- headroom reserved for algot-strategies / algot-core / algot-monitoring --"
kubectl top nodes 2>/dev/null || echo "  (metrics-server not installed; node metrics unavailable)"

printf '\n=== %d passed, %d failed, %d warnings ===\n' "$PASS" "$FAIL" "$WARN"
[[ "$FAIL" -eq 0 ]] || exit 1
