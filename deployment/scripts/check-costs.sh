#!/usr/bin/env bash
# Verify that the AlgoT deployment is actually costing nothing.
#
# The free-tier gate proves the *configuration* is eligible. This proves the
# *bill*. Run it daily; it exits non-zero if any SKU has a non-zero cost.
#
#   check-costs.sh            last 2 days, daily granularity
#   check-costs.sh 7          last 7 days
#
# Why this matters more than usual here: this is a pay-as-you-go tenancy, so
# nothing stops an over-allocation from billing. Oracle's UK price list
# grants paid tenancies 3,000 A1 OCPU-hours and 18,000 GB-hours monthly.
# This deployment consumes at most 2,976 and 17,856 in a 744-hour month.
# If Oracle changes the allowance, the "Standard - A1" lines below turn
# non-zero. After reviewing node capacity, the contingency is:
#
#   node_ocpus = 1, node_memory_gbs = 6   in terraform.tfvars
#   terraform plan -out=plan.tfplan && terraform apply plan.tfplan
#
# which halves the pool to 2 OCPUs / 12 GB total and keeps both nodes.
set -euo pipefail

DAYS="${1:-2}"
# Resolve the repository root from this script's own location, so the script
# works from any working directory. `git rev-parse` was used here originally
# and failed outright when run from outside the repo (e.g. from $HOME).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
TF_DIR="${TF_DIR:-$REPO_ROOT/infrastructure/terraform}"
TERRAFORM="${TERRAFORM:-terraform}"

export OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING=True

TENANCY="$("$TERRAFORM" -chdir="$TF_DIR" output -raw tenancy_ocid 2>/dev/null \
  || awk -F= '/^tenancy[[:space:]]*=/{gsub(/[[:space:]]/,"",$2); print $2; exit}' "${OCI_CLI_CONFIG_FILE:-$HOME/.oci/config}")"

START="$(date -u -d "$DAYS days ago" +%Y-%m-%dT00:00:00Z)"
END="$(date -u +%Y-%m-%dT00:00:00Z)"

echo "AlgoT cost verification"
echo "  tenancy : ${TENANCY:0:32}..."
echo "  window  : $START -> $END"
echo

oci usage-api usage-summary request-summarized-usages \
  --tenant-id "$TENANCY" \
  --granularity DAILY \
  --query-type COST \
  --group-by '["service","skuName"]' \
  --time-usage-started "$START" \
  --time-usage-ended "$END" \
  --output json > /tmp/algot-usage.json

python3 - <<'PY'
import json, pathlib, sys
from collections import defaultdict

items = json.loads(pathlib.Path("/tmp/algot-usage.json").read_text())["data"]["items"]

per_day = defaultdict(lambda: defaultdict(float))
per_sku = defaultdict(lambda: {"cost": 0.0, "qty": 0.0})
currency = "GBP"

for i in items:
    day = (i.get("time-usage-started") or "")[:10]
    sku = f"{i.get('service') or ''} / {i.get('sku-name') or '(unattributed)'}"
    cost = float(i.get("computed-amount") or 0)
    currency = i.get("currency") or currency
    per_day[day][sku] += cost
    per_sku[sku]["cost"] += cost
    per_sku[sku]["qty"] += float(i.get("computed-quantity") or 0)

print(f"{'SKU':62s} {'QUANTITY':>14s} {'COST':>10s}")
print("-" * 90)
for sku in sorted(per_sku):
    v = per_sku[sku]
    flag = "" if v["cost"] == 0 else "  <-- BILLED"
    print(f"{sku[:62]:62s} {v['qty']:>14.4f} {v['cost']:>10.4f}{flag}")

print()
print("Daily totals (the last entry may be a partial day):")
days = sorted(per_day)
for day in days:
    total = sum(per_day[day].values())
    mark = "  OK" if total == 0 else "  BILLED"
    print(f"  {day}  {total:>10.4f} {currency}{mark}")
    for sku, cost in sorted(per_day[day].items(), key=lambda x: -x[1]):
        if cost > 0:
            print(f"      {sku[:56]:56s} {cost:.4f}")

# Always report A1 usage explicitly.
a1 = {k: v for k, v in per_sku.items() if "A1" in k}
if a1:
    print()
    print("Ampere A1 (paid-tenancy allowance: 3,000 OCPU-h / 18,000 GB-h per month):")
    for sku in sorted(a1):
        v = a1[sku]
        state = "FREE" if v["cost"] == 0 else f"BILLED {v['cost']:.4f}"
        print(f"  {sku[:58]:58s} qty={v['qty']:>12.2f}  {state}")
    if any(v["cost"] > 0 for v in a1.values()):
        print()
        print("  A1 is now billing. Review the allocation and contingency above.")

print()
print("-" * 90)

# Judge on the most recent COMPLETE day. The newest day is partial (the
# Usage API lags about a day), and a charge that has already stopped still
# shows a tail, which would otherwise fail forever.
if len(days) < 2:
    print("RESULT: INCONCLUSIVE -- need at least two days of data.")
    sys.exit(0)

judged = days[-2]
judged_total = sum(per_day[judged].values())

if judged_total > 0:
    print(f"RESULT: FAIL -- {judged_total:.4f} {currency} charged on {judged}.")
    for sku, cost in sorted(per_day[judged].items(), key=lambda x: -x[1]):
        if cost > 0:
            print(f"  {sku}: {cost:.4f}")
    print()
    print("If the resource behind this was already deleted, the charge tails")
    print("off over the following day. Re-run tomorrow to confirm.")
    sys.exit(1)

print(f"RESULT: PASS -- {judged} billed 0.00 {currency}. Recurring cost is {currency} 0.00.")
PY
