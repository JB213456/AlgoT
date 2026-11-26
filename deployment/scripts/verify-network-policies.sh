#!/usr/bin/env bash
# Prove NetworkPolicy is ENFORCED, not merely declared.
#
# flannel accepts NetworkPolicy objects and ignores them, so the presence of
# policy objects proves nothing. Each check below asserts an observable
# outcome: a connection that must succeed, and connections that must fail.
# A policy engine that is absent or misconfigured fails these tests.
set -uo pipefail

# Resolve the repository root from this script's own location, so the script
# works from any working directory. `git rev-parse` was used here originally
# and failed outright when run from outside the repo (e.g. from $HOME).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

NS=algot-data
DB_HOST=market-data-db.algot-data.svc.cluster.local
DB_PORT=8000
PASS=0
FAIL=0
TOOLBOX="${TOOLBOX_IMAGE:-$(python3 -c "
import json;print(json.load(open('$REPO_ROOT/deployment/manifests/arm64-digests.json'))['toolbox'])" 2>/dev/null)}"

[[ -n "$TOOLBOX" ]] || { echo "FATAL: toolbox image unknown; run build-arm64-images.sh" >&2; exit 1; }

cleanup() {
  kubectl -n "$NS" delete pod policy-probe-allowed policy-probe-denied --ignore-not-found --wait=false >/dev/null 2>&1
}
trap cleanup EXIT
cleanup
kubectl -n "$NS" wait --for=delete pod/policy-probe-allowed --timeout=60s >/dev/null 2>&1 || true
kubectl -n "$NS" wait --for=delete pod/policy-probe-denied --timeout=60s >/dev/null 2>&1 || true

record() {
  local name="$1" expected="$2" actual="$3" detail="${4:-}"
  if [[ "$expected" == "$actual" ]]; then
    printf '  PASS  %-52s (%s)\n' "$name" "$actual"
    PASS=$((PASS + 1))
  else
    printf '  FAIL  %-52s expected %s, got %s %s\n' "$name" "$expected" "$actual" "$detail"
    FAIL=$((FAIL + 1))
  fi
}

# Runs a TCP connect probe from a one-shot pod and prints reachable/blocked.
# A plain manifest is used rather than `kubectl run --overrides` so the pod
# spec (labels, restricted securityContext, pull secret) is unambiguous.
probe() {
  local pod_name="$1" namespace="$2" label_key="$3" label_value="$4" host="$5" port="$6"

  kubectl -n "$namespace" delete pod "$pod_name" --ignore-not-found --wait=true >/dev/null 2>&1

  kubectl apply -f - >/dev/null 2>&1 <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $pod_name
  namespace: $namespace
  labels:
    $label_key: "$label_value"
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  imagePullSecrets:
    - name: ocir-pull
  securityContext:
    runAsNonRoot: true
    runAsUser: 10002
    runAsGroup: 10002
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: probe
      image: $TOOLBOX
      command:
        - python3
        - -c
        - |
          import socket, sys
          s = socket.socket()
          s.settimeout(6)
          try:
              s.connect((sys.argv[1], int(sys.argv[2])))
              print("reachable")
          except Exception:
              print("blocked")
        - "$host"
        - "$port"
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: [ALL]
      resources:
        requests:
          cpu: 25m
          memory: 64Mi
        limits:
          cpu: 200m
          memory: 128Mi
YAML

  # Wait for the pod to finish rather than to be Ready: it exits immediately.
  local waited=0
  while [[ $waited -lt 150 ]]; do
    local phase
    phase=$(kubectl -n "$namespace" get pod "$pod_name" -o jsonpath='{.status.phase}' 2>/dev/null)
    [[ "$phase" == "Succeeded" || "$phase" == "Failed" ]] && break
    sleep 3
    waited=$((waited + 3))
  done

  local result
  result=$(kubectl -n "$namespace" logs "$pod_name" --tail=5 2>/dev/null \
            | tr -d '\r' | grep -Eo 'reachable|blocked' | tail -1)
  echo "${result:-inconclusive}"
}

echo "=== NetworkPolicy enforcement verification ==="
echo

echo "Policy engine:"
if kubectl -n kube-system get daemonset calico-node >/dev/null 2>&1; then
  READY=$(kubectl -n kube-system get daemonset calico-node -o jsonpath='{.status.numberReady}')
  DESIRED=$(kubectl -n kube-system get daemonset calico-node -o jsonpath='{.status.desiredNumberScheduled}')
  echo "  calico-node ready on $READY/$DESIRED nodes"
  [[ "$READY" == "$DESIRED" && "$READY" != "0" ]] || { echo "  FAIL: calico-node is not ready on every node" >&2; FAIL=$((FAIL+1)); }
else
  echo "  FAIL: no calico-node daemonset -- NetworkPolicy will NOT be enforced" >&2
  FAIL=$((FAIL + 1))
fi
echo "  policies in $NS: $(kubectl -n "$NS" get networkpolicy --no-headers 2>/dev/null | wc -l | tr -d ' ')"
echo

echo "Checks:"

# 1. A labelled client is permitted to reach the database.
record "labelled client -> database:8000" reachable \
  "$(probe policy-probe-allowed "$NS" "algot.io/database-client" "true" "$DB_HOST" "$DB_PORT")"

# 2. An unlabelled pod in the same namespace must NOT reach the database.
#    This is the core assertion: default-deny plus the database-ingress
#    podSelector. If the engine is missing, this returns "reachable".
record "unlabelled same-namespace pod -> database:8000" blocked \
  "$(probe policy-probe-denied "$NS" "role" "unauthorised" "$DB_HOST" "$DB_PORT")"

# 3. The database must not be able to reach the public internet.
echo -n "  ....  database -> api.alpaca.markets:443 "
DB_POD=$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=market-data-db -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [[ -n "$DB_POD" ]]; then
  EGRESS=$(kubectl -n "$NS" exec "$DB_POD" -- python3 -c "
import socket
s=socket.socket(); s.settimeout(6)
try:
    s.connect(('api.alpaca.markets',443)); print('reachable')
except Exception:
    print('blocked')
" 2>/dev/null | tr -d '\r' | grep -Eo 'reachable|blocked' | tail -1)
  echo
  record "database -> internet (must be denied)" blocked "${EGRESS:-inconclusive}"
else
  echo; echo "  SKIP  database pod not found"
fi

# 4. Ingestion is permitted outbound HTTPS to its providers.
ING_POD=$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=data-ingestion -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [[ -n "$ING_POD" ]]; then
  OUT=$(kubectl -n "$NS" exec "$ING_POD" -c live-ingestion -- python3 -c "
import socket
s=socket.socket(); s.settimeout(6)
try:
    s.connect(('api.alpaca.markets',443)); print('reachable')
except Exception:
    print('blocked')
" 2>/dev/null | tr -d '\r' | grep -Eo 'reachable|blocked' | tail -1)
  record "ingestion -> Alpaca:443 (must be allowed)" reachable "${OUT:-inconclusive}"
fi

echo
echo "=== $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]] || {
  echo "NetworkPolicy enforcement is NOT verified." >&2
  exit 1
}
echo "NetworkPolicy enforcement verified."
