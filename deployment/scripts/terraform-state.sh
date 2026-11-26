#!/usr/bin/env bash
# Sync Terraform state to the private, versioned OCI Object Storage bucket.
#
# Why not the S3-compatible backend: it requires a long-lived Customer Secret
# Key. This keeps the operator's existing API key as the only credential.
# The bucket has versioning enabled, so every push is recoverable.
#
#   terraform-state.sh pull          fetch remote state before planning
#   terraform-state.sh push          upload state after applying
#   terraform-state.sh lock|unlock   advisory lock around an apply
#   terraform-state.sh versions      list recoverable state versions
#   terraform-state.sh restore <id>  restore a specific version
set -euo pipefail

# Resolve the repository root from this script's own location, so the script
# works from any working directory. `git rev-parse` was used here originally
# and failed outright when run from outside the repo (e.g. from $HOME).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
TF_DIR="${TF_DIR:-$REPO_ROOT/infrastructure/terraform}"
BUCKET="${STATE_BUCKET:-algot-terraform-state}"
OBJECT="${STATE_OBJECT:-algot/terraform.tfstate}"
LOCK_OBJECT="${OBJECT}.lock"
STATE_FILE="$TF_DIR/terraform.tfstate"

export OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING=True

NAMESPACE="${OCI_NAMESPACE:-$(oci os ns get --query data --raw-output)}"
REGION="${OCI_CLI_REGION:-$(oci iam region-subscription list --query 'data[0]."region-name"' --raw-output 2>/dev/null || echo uk-london-1)}"

# Object Storage REST endpoint, used for the one operation the CLI cannot
# express: a conditional create (see `lock` below).
OS_ENDPOINT="https://objectstorage.${REGION}.oraclecloud.com"

# URL-encode the "/" in the object path for the REST URI.
LOCK_URI="${OS_ENDPOINT}/n/${NAMESPACE}/b/${BUCKET}/o/${LOCK_OBJECT//\//%2F}"

TMPDIR_RUN="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_RUN"' EXIT

usage() { sed -n '2,14p' "$0" >&2; exit 2; }

case "${1:-}" in
  pull)
    if oci os object get --namespace "$NAMESPACE" --bucket-name "$BUCKET" \
         --name "$OBJECT" --file "$STATE_FILE.remote" 2>/dev/null; then
      if [[ -f "$STATE_FILE" ]] && ! cmp -s "$STATE_FILE" "$STATE_FILE.remote"; then
        # Never clobber local state silently: it may be the newer copy.
        cp "$STATE_FILE" "$STATE_FILE.local.$(date -u +%Y%m%dT%H%M%SZ)"
        echo "Local state differed; previous copy kept alongside it." >&2
      fi
      mv "$STATE_FILE.remote" "$STATE_FILE"
      echo "Pulled state from oci://$BUCKET/$OBJECT"
    else
      echo "No remote state yet; treating local state as authoritative."
    fi
    ;;

  push)
    [[ -f "$STATE_FILE" ]] || { echo "No local state at $STATE_FILE" >&2; exit 1; }
    oci os object put --namespace "$NAMESPACE" --bucket-name "$BUCKET" \
      --name "$OBJECT" --file "$STATE_FILE" --force >/dev/null
    echo "Pushed state to oci://$BUCKET/$OBJECT"
    ;;

  lock)
    # An If-None-Match: * PUT is a conditional create: the service returns
    # 412 if the object already exists, which is what makes the lock atomic
    # rather than a check-then-write race.
    #
    # This goes through `oci raw-request` because `oci os object put` has no
    # --if-none-match option. The earlier implementation passed that flag and
    # so failed on every invocation, reporting "locked by another operator"
    # when no lock object existed at all -- which blocked every apply.
    BODY="$(printf '{"holder":"%s","host":"%s","at":"%s"}' \
      "${USER:-unknown}" "$(hostname)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"

    # raw-request ignores --query, so the HTTP status is read out of the
    # response document rather than selected by the CLI.
    oci raw-request --http-method PUT --target-uri "$LOCK_URI" \
      --request-body "$BODY" --request-headers '{"if-none-match":"*"}' \
      > "$TMPDIR_RUN/lock-response.json" 2>"$TMPDIR_RUN/lock-error.txt" || true

    STATUS="$(python3 -c '
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("status", "no status in response"))
except Exception:
    print("request failed")
' "$TMPDIR_RUN/lock-response.json")"

    case "$STATUS" in
      2*)
        echo "Acquired state lock."
        ;;
      412*)
        echo "State is locked by another operator:" >&2
        oci os object get --namespace "$NAMESPACE" --bucket-name "$BUCKET" \
          --name "$LOCK_OBJECT" --file "$TMPDIR_RUN/held.json" >/dev/null 2>&1 \
          && cat "$TMPDIR_RUN/held.json" >&2
        echo >&2
        echo "If that holder is gone, release it with: $0 unlock" >&2
        exit 1
        ;;
      *)
        echo "Could not acquire the state lock (status: $STATUS)." >&2
        exit 1
        ;;
    esac
    ;;

  unlock)
    oci os object delete --namespace "$NAMESPACE" --bucket-name "$BUCKET" \
      --object-name "$LOCK_OBJECT" --force >/dev/null 2>&1 || true
    echo "Released state lock."
    ;;

  versions)
    # Filtered to the exact object name: --prefix alone also matches
    # "$OBJECT.lock", which would list lock versions as if they were
    # recoverable state.
    # --all: without it only the first page of versions is listed, which would
    # silently hide older recoverable state from the operator who needs it.
    oci os object list-object-versions --namespace "$NAMESPACE" \
      --bucket-name "$BUCKET" --prefix "$OBJECT" --all \
      --query "data[?name=='$OBJECT'].{version:\"version-id\",modified:\"time-modified\",bytes:size}" \
      --output table
    ;;

  restore)
    VERSION="${2:-}"
    [[ -n "$VERSION" ]] || { echo "Usage: $0 restore <version-id>" >&2; exit 2; }
    cp "$STATE_FILE" "$STATE_FILE.before-restore" 2>/dev/null || true
    oci os object get --namespace "$NAMESPACE" --bucket-name "$BUCKET" \
      --name "$OBJECT" --version-id "$VERSION" --file "$STATE_FILE"
    echo "Restored state version $VERSION. Run 'terraform plan' and confirm it is empty."
    ;;

  *) usage ;;
esac
