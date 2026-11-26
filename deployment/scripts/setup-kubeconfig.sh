#!/usr/bin/env bash
# Generate a kubeconfig for the AlgoT OKE cluster.
#
# The generated file holds no long-lived token: it shells out to the OCI CLI
# for a short-lived exec credential on every call, so the operator's API key
# stays the only secret. It is written outside the repository by default and
# the repo's .gitignore covers the kubeconfig name regardless.
set -euo pipefail

# Resolve the repository root from this script's own location, so the script
# works from any working directory. `git rev-parse` was used here originally
# and failed outright when run from outside the repo (e.g. from $HOME).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

TF_DIR="${TF_DIR:-$REPO_ROOT/infrastructure/terraform}"
KUBECONFIG_PATH="${KUBECONFIG_PATH:-$HOME/.kube/algot-oke.config}"
TERRAFORM="${TERRAFORM:-terraform}"

export OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING=True

CLUSTER_ID="$("$TERRAFORM" -chdir="$TF_DIR" output -raw cluster_id)"
REGION="$("$TERRAFORM" -chdir="$TF_DIR" output -raw region)"

mkdir -p "$(dirname "$KUBECONFIG_PATH")"

echo "Generating kubeconfig for cluster $CLUSTER_ID"
oci ce cluster create-kubeconfig \
  --cluster-id "$CLUSTER_ID" \
  --file "$KUBECONFIG_PATH" \
  --region "$REGION" \
  --token-version 2.0.0 \
  --kube-endpoint PUBLIC_ENDPOINT \
  --overwrite

chmod 600 "$KUBECONFIG_PATH"

echo
echo "Wrote $KUBECONFIG_PATH"
echo "Use it with:  export KUBECONFIG=$KUBECONFIG_PATH"
echo
if kubectl --kubeconfig "$KUBECONFIG_PATH" version --request-timeout=20s >/dev/null 2>&1; then
  echo "API reachable."
  kubectl --kubeconfig "$KUBECONFIG_PATH" get nodes -o wide
else
  cat >&2 <<'MSG'
Could not reach the Kubernetes API.

The API endpoint only accepts the CIDRs in `api_allowed_cidrs`. If your public
address has changed, update it in terraform.tfvars and re-apply:

    curl -s https://checkip.amazonaws.com
    # set api_allowed_cidrs = ["<that address>/32"] and run terraform apply

MSG
  exit 1
fi
