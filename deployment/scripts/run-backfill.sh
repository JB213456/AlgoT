#!/usr/bin/env bash
# Launch a historical backfill as a Kubernetes Job.
#
#   run-backfill.sh --symbol AAPL --provider alpaca --start 2026-01-01
#   run-backfill.sh --symbol VOD.L --provider lse
#   run-backfill.sh --symbol AAPL --coverage
#
# Every argument is passed straight through to the market-backfill CLI.
# The alternative, for a quick run against the live pod's own coverage store:
#   kubectl exec -n algot-data deployment/data-ingestion -c live-ingestion -- \
#     market-backfill --symbol AAPL
set -euo pipefail

# Resolve the repository root from this script's own location, so the script
# works from any working directory. `git rev-parse` was used here originally
# and failed outright when run from outside the repo (e.g. from $HOME).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
NS=algot-data
TEMPLATE="$REPO_ROOT/kubernetes/ingestion/backfill-job.yaml"
TF_DIR="${TF_DIR:-$REPO_ROOT/infrastructure/terraform}"
TERRAFORM="${TERRAFORM:-terraform}"
DIGESTS="${DIGEST_FILE:-$REPO_ROOT/deployment/manifests/arm64-digests.json}"
FOLLOW="${FOLLOW:-1}"

[[ $# -gt 0 ]] || { echo "Usage: $0 --symbol SYMBOL [--provider alpaca|lse|all] [--start YYYY-MM-DD] [--end YYYY-MM-DD] [...]" >&2; exit 2; }

export OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING=True

INGESTION_IMAGE="$(python3 -c "import json;print(json.load(open('$DIGESTS'))['ingestion'])")"
TOOLBOX_IMAGE="$(python3 -c "import json;print(json.load(open('$DIGESTS'))['toolbox'])")"
OCI_NAMESPACE="$("$TERRAFORM" -chdir="$TF_DIR" output -raw objectstorage_namespace)"

# Only one backfill at a time: concurrent historical downloads would compete
# with live ingestion for the node's CPU and breach provider rate limits.
RUNNING=$(kubectl -n "$NS" get jobs -l app.kubernetes.io/name=data-backfill \
  -o jsonpath='{range .items[?(@.status.active>0)]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep -c . || true)
if [[ "${RUNNING:-0}" -gt 0 ]]; then
  echo "A backfill Job is already active. Wait for it or delete it first:" >&2
  kubectl -n "$NS" get jobs -l app.kubernetes.io/name=data-backfill >&2
  exit 1
fi

# Render the template, then append the CLI arguments to the container args.
RENDERED="$(mktemp)"
trap 'rm -f "$RENDERED"' EXIT

sed -e "s|__INGESTION_IMAGE__|$INGESTION_IMAGE|g" \
    -e "s|__TOOLBOX_IMAGE__|$TOOLBOX_IMAGE|g" \
    -e "s|__OCI_NAMESPACE__|$OCI_NAMESPACE|g" \
    "$TEMPLATE" > "$RENDERED"

python3 - "$RENDERED" "$@" <<'PY'
import sys, pathlib, yaml

path = pathlib.Path(sys.argv[1])
cli_args = sys.argv[2:]
doc = yaml.safe_load(path.read_text())

containers = doc["spec"]["template"]["spec"]["containers"]
backfill = next(c for c in containers if c["name"] == "backfill")
# args[0] is the shell script, args[1] is $0; the CLI arguments follow.
backfill["args"] = backfill["args"] + cli_args

path.write_text(yaml.safe_dump(doc, default_flow_style=False, sort_keys=False))
print("backfill arguments: " + " ".join(cli_args))
PY

echo "==> Creating backfill Job"
JOB_NAME="$(kubectl -n "$NS" create -f "$RENDERED" -o jsonpath='{.metadata.name}')"
echo "    job/$JOB_NAME"

if [[ "$FOLLOW" != "1" ]]; then
  echo "Follow it with: kubectl -n $NS logs -f job/$JOB_NAME -c backfill"
  exit 0
fi

echo "==> Waiting for the pod to start"
for _ in $(seq 1 60); do
  POD="$(kubectl -n "$NS" get pod -l "job-name=$JOB_NAME" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [[ -n "$POD" ]] && break
  sleep 3
done
[[ -n "${POD:-}" ]] || { echo "Pod never appeared; inspect: kubectl -n $NS describe job/$JOB_NAME" >&2; exit 1; }

kubectl -n "$NS" wait --for=condition=Initialized "pod/$POD" --timeout=300s >/dev/null 2>&1 || true
kubectl -n "$NS" logs -f "$POD" -c backfill || true

echo
echo "==> Result"
kubectl -n "$NS" get job "$JOB_NAME" -o wide
SUCCEEDED="$(kubectl -n "$NS" get job "$JOB_NAME" -o jsonpath='{.status.succeeded}' 2>/dev/null)"
if [[ "${SUCCEEDED:-0}" == "1" ]]; then
  echo "Backfill completed. Coverage snapshot uploaded to object storage."
else
  echo "Backfill did not report success." >&2
  kubectl -n "$NS" describe job "$JOB_NAME" | tail -20 >&2
  exit 1
fi
