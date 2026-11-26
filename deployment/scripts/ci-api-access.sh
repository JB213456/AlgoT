#!/usr/bin/env bash
# Temporarily allow a CI runner to reach the Kubernetes API, then withdraw it.
#
# The API endpoint's NSG allows only an operator allowlist. GitHub-hosted
# runners have unpredictable addresses, and GitHub's published ranges are far
# too numerous for an NSG, so widening the allowlist permanently would mean
# effectively opening 6443 to the internet. Instead a deployment grants
# exactly its own /32 for the duration of the job and revokes it afterwards.
#
#   ci-api-access.sh grant    adds a rule for this runner's address
#   ci-api-access.sh revoke   removes every rule this script added
set -euo pipefail

# Resolve the repository root from this script's own location, so the script
# works from any working directory. `git rev-parse` was used here originally
# and failed outright when run from outside the repo (e.g. from $HOME).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

ACTION="${1:-}"
TF_DIR="${TF_DIR:-$REPO_ROOT/infrastructure/terraform}"
TERRAFORM="${TERRAFORM:-terraform}"
# Marks rules this script owns so revoke never removes an operator rule.
DESCRIPTION="ci-temporary-kubectl-access"

export OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING=True

NSG_ID="$("$TERRAFORM" -chdir="$TF_DIR" output -raw k8s_api_nsg_id)"

case "$ACTION" in
  grant)
    IP="$(curl -fsS --max-time 20 https://checkip.amazonaws.com | tr -d '[:space:]')"
    [[ "$IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "FATAL: could not determine runner address" >&2; exit 1; }
    echo "Granting $IP/32 access to the Kubernetes API"
    oci network nsg rules add \
      --nsg-id "$NSG_ID" \
      --security-rules "$(python3 -c "
import json,sys
print(json.dumps([{
  'direction': 'INGRESS',
  'protocol': '6',
  'source': sys.argv[1] + '/32',
  'sourceType': 'CIDR_BLOCK',
  'description': sys.argv[2],
  'isStateless': False,
  'tcpOptions': {'destinationPortRange': {'min': 6443, 'max': 6443}},
}]))
" "$IP" "$DESCRIPTION")" >/dev/null
    echo "Granted."
    ;;

  revoke)
    echo "Revoking temporary CI access rules"
    IDS="$(oci network nsg rules list --nsg-id "$NSG_ID" --all \
      --query "data[?description=='$DESCRIPTION'].id" --raw-output 2>/dev/null || echo '[]')"
    COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$IDS")"
    if [[ "$COUNT" == "0" ]]; then
      echo "No temporary rules to remove."
      exit 0
    fi
    oci network nsg rules remove --nsg-id "$NSG_ID" --security-rule-ids "$IDS" >/dev/null
    echo "Removed $COUNT temporary rule(s)."
    ;;

  *)
    echo "Usage: $0 grant|revoke" >&2
    exit 2
    ;;
esac
