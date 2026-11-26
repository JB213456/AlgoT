#!/usr/bin/env bash
# Infrastructure drift detection for the AlgoT OCI deployment.
#
# Read-only: it never creates, changes or deletes anything, and it takes no
# state lock, so it is safe to run on a schedule alongside normal work.
#
# It looks for drift in three different directions, because `terraform plan`
# alone only sees one of them:
#
#   1. CONFIG vs REALITY   -- a plan against live infrastructure. Catches a
#                             resource changed in the console, or a config
#                             change that was committed but never applied.
#   2. REALITY vs STATE    -- a sweep for resources that exist in the
#                             compartment but are in no Terraform state file.
#                             A plan is blind to these: Terraform cannot drift
#                             on a resource it has never heard of, and this is
#                             how a billable resource appears unnoticed.
#   3. THE BILL            -- delegated to check-costs.sh. The only check that
#                             catches a paid resource outside this compartment
#                             or a free allowance that has been exceeded.
#
# Exit codes:
#   0  no drift, nothing unmanaged, no charges
#   1  the check itself could not run
#   2  drift, unmanaged resources or charges found
#
#   drift-detect.sh              all three checks
#   drift-detect.sh plan         Terraform plan drift only
#   drift-detect.sh unmanaged    unmanaged-resource sweep only
#   drift-detect.sh costs        billing check only
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
TF_DIR="${TF_DIR:-$REPO_ROOT/infrastructure/terraform}"
POLICY_DIR="$TF_DIR/policies/free-tier"
STATE_FILE="$TF_DIR/terraform.tfstate"

export OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING=True

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FINDINGS=0
note()  { printf '%s\n' "$*"; }
head2() { printf '\n%s\n%s\n' "$*" "$(printf '=%.0s' $(seq 1 ${#1}))"; }
fail()  { FINDINGS=1; printf '  DRIFT: %s\n' "$*"; }

# Honour GitHub Actions annotations when running in CI.
if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
  annotate() { printf '::warning::%s\n' "$*"; }
else
  annotate() { :; }
fi

# --- the compartment and region under management ----------------------------
resolve_context() {
  COMPARTMENT="${TF_VAR_compartment_ocid:-}"
  if [[ -z "$COMPARTMENT" && -f "$TF_DIR/terraform.tfvars" ]]; then
    COMPARTMENT="$(sed -n 's/^[[:space:]]*compartment_ocid[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
      "$TF_DIR/terraform.tfvars" | head -1)"
  fi
  if [[ -z "$COMPARTMENT" ]]; then
    note "Cannot determine the compartment: set TF_VAR_compartment_ocid." >&2
    return 1
  fi
  # Exported so the OCI CLI calls below all target the managed region, even
  # when the operator's profile defaults elsewhere.
  OCI_CLI_REGION="${OCI_CLI_REGION:-${TF_VAR_region:-uk-london-1}}"
  export OCI_CLI_REGION

  # File Storage can only be listed per availability domain.
  AD="${TF_VAR_availability_domain:-}"
  if [[ -z "$AD" && -f "$TF_DIR/terraform.tfvars" ]]; then
    AD="$(sed -n 's/^[[:space:]]*availability_domain[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
      "$TF_DIR/terraform.tfvars" | head -1)"
  fi
}

# ---------------------------------------------------------------------------
# 1. CONFIG vs REALITY
# ---------------------------------------------------------------------------
check_plan() {
  head2 "1. Terraform plan against live infrastructure"

  if ! terraform -chdir="$TF_DIR" init -input=false -upgrade=false >/dev/null 2>&1; then
    note "  terraform init failed." >&2
    return 1
  fi

  # -lock=false: this is a read-only plan and must never block an operator's
  # apply. -detailed-exitcode: 0 = empty plan, 2 = changes pending.
  terraform -chdir="$TF_DIR" plan -input=false -lock=false -detailed-exitcode \
    -out="$WORK/drift.tfplan" > "$WORK/plan.txt" 2>&1
  local code=$?

  case "$code" in
    0)
      note "  No drift: live infrastructure matches the configuration."
      ;;
    2)
      fail "the plan is not empty -- configuration and reality disagree."
      annotate "Terraform drift detected in the AlgoT OCI deployment"
      note ""
      # The change summary, without the refresh noise.
      grep -E '^(  # |Plan:|Changes to Outputs)' "$WORK/plan.txt" | sed 's/^/  /'
      ;;
    *)
      note "  terraform plan failed (exit $code):" >&2
      tail -25 "$WORK/plan.txt" >&2
      return 1
      ;;
  esac

  # Even a drifting plan is worth gating: it says whether what Terraform now
  # wants to build is still inside the Always Free envelope.
  if [[ -f "$WORK/drift.tfplan" ]]; then
    terraform -chdir="$TF_DIR" show -json "$WORK/drift.tfplan" > "$WORK/drift.json" 2>/dev/null
    note ""
    if python3 "$POLICY_DIR/check_plan.py" "$WORK/drift.json" \
         --allowlist "$POLICY_DIR/allowlist.yaml" | sed 's/^/  /'; then
      :
    else
      fail "the free-tier gate rejects the current planned state."
      annotate "AlgoT infrastructure is no longer provably Always Free eligible"
    fi
  fi
}

# ---------------------------------------------------------------------------
# 2. REALITY vs STATE
# ---------------------------------------------------------------------------
# Every OCID the state file knows about, one per line.
state_ocids() {
  python3 - "$STATE_FILE" <<'PY'
import json, re, sys
try:
    text = open(sys.argv[1]).read()
except OSError:
    sys.exit(0)
# Matching OCIDs across the whole document rather than walking resources
# catches ids held in nested attributes too (node ids inside a node pool,
# for instance), which is exactly what must not be reported as unmanaged.
for ocid in set(re.findall(r'ocid1\.[a-z0-9]+\.[a-z0-9\-\.]*[a-z0-9]{10,}', text)):
    print(ocid)
PY
}

check_unmanaged() {
  head2 "2. Resources in the compartment that Terraform does not manage"
  resolve_context || return 1

  if [[ ! -f "$STATE_FILE" ]]; then
    note "  No local state at $STATE_FILE -- pull it first." >&2
    return 1
  fi

  state_ocids | sort -u > "$WORK/state-ocids.txt"
  note "  State tracks $(wc -l < "$WORK/state-ocids.txt") OCIDs."

  # "description|OCI CLI arguments". Each command lists one resource type in
  # the compartment and prints `<ocid>\t<name>` per live resource.
  local -a probes=(
    "compute instance|compute instance list -c $COMPARTMENT --all"
    "block volume|bv volume list -c $COMPARTMENT --all"
    "OKE cluster|ce cluster list -c $COMPARTMENT --all"
    "OKE node pool|ce node-pool list -c $COMPARTMENT --all"
    "VCN|network vcn list -c $COMPARTMENT --all"
    "subnet|network subnet list -c $COMPARTMENT --all"
    "network security group|network nsg list -c $COMPARTMENT --all"
    "route table|network route-table list -c $COMPARTMENT --all"
    "internet gateway|network internet-gateway list -c $COMPARTMENT --all"
    "NAT gateway|network nat-gateway list -c $COMPARTMENT --all"
    "service gateway|network service-gateway list -c $COMPARTMENT --all"
    "load balancer|lb load-balancer list -c $COMPARTMENT --all"
    "network load balancer|nlb network-load-balancer list -c $COMPARTMENT --all"
    "vault|kms management vault list -c $COMPARTMENT --all"
    "container repository|artifacts container repository list -c $COMPARTMENT --all"
    "autonomous database|db autonomous-database list -c $COMPARTMENT --all"
  )
  # File Storage has no Always Free allowance at all, so a file system in this
  # compartment is both unmanaged and billing. Only listable per AD.
  if [[ -n "${AD:-}" ]]; then
    probes+=("file system|fs file-system list -c $COMPARTMENT --all --availability-domain $AD")
  fi

  # A helper file rather than `python3 -c`: the parser needs both single and
  # double quotes, and inlining it inside a single-quoted shell argument made
  # it an unparseable Python snippet that printed nothing -- so the sweep
  # reported "nothing unmanaged" however many resources were live.
  cat > "$WORK/rows.py" <<'PYEOF'
import json
import sys

try:
    rows = json.load(open(sys.argv[1])) or []
except (OSError, ValueError):
    sys.exit(0)

for row in rows:
    ocid = row[0] if len(row) > 0 and row[0] else ""
    name = row[1] if len(row) > 1 and row[1] else ""
    state = row[2] if len(row) > 2 and row[2] else ""
    if ocid:
        print(ocid + "\t" + name + "\t" + state)
PYEOF

  local probe desc args found=0
  for probe in "${probes[@]}"; do
    desc="${probe%%|*}"; args="${probe#*|}"
    # shellcheck disable=SC2086
    # Resources already on their way out are reported separately below rather
    # than filtered away: a vault in PENDING_DELETION is not something to
    # import or act on, but a scheduled deletion can be cancelled, so it is
    # worth seeing that it is still there.
    if ! oci $args --query "data[?\"lifecycle-state\"!='TERMINATED' && \"lifecycle-state\"!='DELETED' && \"lifecycle-state\"!='TERMINATING' && \"lifecycle-state\"!='DELETING'].[id,\"display-name\",\"lifecycle-state\"]" \
          --output json > "$WORK/live.json" 2>"$WORK/live.err"; then
      # A service that is not subscribed, or that this principal may not
      # read, is not drift -- it just cannot be swept.
      note "  (skipped $desc: $(head -1 "$WORK/live.err" 2>/dev/null || echo 'query failed'))"
      continue
    fi

    while IFS=$'\t' read -r ocid name lstate; do
      [[ -z "$ocid" ]] && continue
      grep -qxF "$ocid" "$WORK/state-ocids.txt" && continue
      case "$lstate" in
        PENDING_DELETION|SCHEDULING_DELETION|CANCELLING_DELETION)
          printf '  note: %s %s is unmanaged but already %s -- no action needed (%s)\n' \
            "$desc" "${name:-<unnamed>}" "$lstate" "$ocid"
          ;;
        *)
          fail "unmanaged $desc: ${name:-<unnamed>} [${lstate:-unknown}] ($ocid)"
          found=1
          ;;
      esac
    done < <(python3 "$WORK/rows.py" "$WORK/live.json")
  done

  if [[ "$found" -eq 0 ]]; then
    note "  Nothing unmanaged: every live resource in the compartment is in Terraform state."
  else
    annotate "Unmanaged OCI resources found in the AlgoT compartment"
    note ""
    note "  Import anything that belongs to this deployment:"
    note "    terraform -chdir=infrastructure/terraform import <address> <ocid>"
    note "  Anything that does not belong should be removed -- it may be billing."
  fi
}

# ---------------------------------------------------------------------------
# 3. THE BILL
# ---------------------------------------------------------------------------
check_costs() {
  head2 "3. Billing"
  if bash "$SCRIPT_DIR/check-costs.sh" "${COST_DAYS:-3}" 2>&1 | sed 's/^/  /'; then
    note "  Billing check passed."
  else
    fail "the tenancy recorded a charge -- see the breakdown above."
    annotate "AlgoT tenancy recorded a non-zero charge"
  fi
}

case "${1:-all}" in
  plan)      check_plan || exit 1 ;;
  unmanaged) check_unmanaged || exit 1 ;;
  costs)     resolve_context; check_costs || exit 1 ;;
  all)
    rc=0
    check_plan      || rc=1
    check_unmanaged || rc=1
    resolve_context && { check_costs || rc=1; }
    [[ "$rc" -eq 1 ]] && { note ""; note "One or more checks could not run."; exit 1; }
    ;;
  *) sed -n '2,30p' "$0" >&2; exit 2 ;;
esac

head2 "Result"
if [[ "$FINDINGS" -eq 0 ]]; then
  note "  No drift detected."
  exit 0
fi
note "  Drift detected. Nothing was changed -- this check is read-only."
exit 2
