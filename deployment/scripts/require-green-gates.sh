#!/usr/bin/env bash
# Refuse to deploy a commit whose mandatory gates did not pass.
#
# The deployment workflows are dispatch-only, and a dispatch can name any ref.
# Without this check, "deploy" means "apply whatever is at that ref", and the
# security, testing and free-tier gates become advisory: they would run on the
# pull request and then have no bearing on what actually reaches the cluster.
#
# So the gates are re-checked here, against the exact commit being deployed,
# by asking the Actions API what concluded for that SHA. A gate that never ran
# is as disqualifying as one that failed -- "no result" must not read as
# "nothing wrong".
#
# Requires: gh with GH_TOKEN, and `actions: read` on that token.
#
# Usage: require-green-gates.sh <sha> [<workflow file> ...]
# Exit:  0 every required gate succeeded, 1 one did not, 2 usage/API problem.
set -uo pipefail

SHA="${1:-}"
if [[ -z "$SHA" ]]; then
  echo "usage: $0 <sha> [<workflow file> ...]" >&2
  exit 2
fi
shift

# The default set is every gate that must hold before anything reaches the
# cluster. Each is a workflow FILE NAME, which is stable across renames of
# the workflow's display name.
if [[ "$#" -gt 0 ]]; then
  REQUIRED=("$@")
else
  REQUIRED=(
    monorepo-pipeline.yml           # service tests, lint, SAST, dependency audit
    infrastructure-validate.yml     # terraform fmt/validate, Checkov, free-tier policy tests
    kubernetes-validate.yml         # manifest schema, cost and data-protection invariants
    kubernetes-security.yml         # RBAC, token, network, hardening, Pod Security admission
    database-migration-validate.yml # schema chain is append-only and non-destructive
  )
fi

command -v gh >/dev/null || { echo "FATAL: gh is not on PATH" >&2; exit 2; }
command -v jq >/dev/null || { echo "FATAL: jq is not on PATH" >&2; exit 2; }

echo "Checking mandatory gates for commit $SHA"
echo

FAILED=0

for workflow in "${REQUIRED[@]}"; do
  # Runs are queried per workflow and filtered to this SHA. --json would be
  # simpler but `gh run list` cannot filter by commit, so the API is used
  # directly.
  response="$(gh api --paginate \
    "repos/{owner}/{repo}/actions/workflows/${workflow}/runs?head_sha=${SHA}&per_page=100" \
    2>/dev/null)" || {
    # A workflow that does not exist in the repository is a configuration
    # error in this list, not a passing gate.
    printf '  %-34s FATAL  cannot query runs (workflow missing, or no actions:read)\n' "$workflow"
    FAILED=$((FAILED + 1))
    continue
  }

  # Only completed runs count. The newest completed run for the SHA wins, so
  # a re-run that fixed a flake is honoured.
  verdict="$(printf '%s' "$response" | jq -r '
    [ .workflow_runs[]? | select(.status == "completed") ]
    | sort_by(.updated_at) | reverse | .[0]
    | if . == null then "none" else "\(.conclusion) \(.html_url)" end
  ' 2>/dev/null)"

  conclusion="${verdict%% *}"
  url="${verdict#* }"

  case "$conclusion" in
    success)
      printf '  %-34s PASS\n' "$workflow"
      ;;
    none)
      # This is the case that matters most: path filters mean a gate may
      # genuinely not have been triggered for this commit. That still means
      # nobody has evidence it holds for what is about to be deployed.
      printf '  %-34s MISSING  no completed run for this commit\n' "$workflow"
      FAILED=$((FAILED + 1))
      ;;
    skipped)
      printf '  %-34s SKIPPED  the run was skipped, so the gate did not evaluate\n' "$workflow"
      FAILED=$((FAILED + 1))
      ;;
    *)
      printf '  %-34s FAIL  conclusion=%s\n' "$workflow" "$conclusion"
      printf '  %-34s       %s\n' "" "$url"
      FAILED=$((FAILED + 1))
      ;;
  esac
done

echo
if (( FAILED > 0 )); then
  cat >&2 <<MSG
REFUSED: $FAILED mandatory gate(s) did not pass for $SHA.

Deploy a commit whose gates are green. If a gate was not triggered for this
commit because of a path filter, dispatch that workflow against this ref so
there is a recorded result, then dispatch the deployment again.
MSG
  exit 1
fi

echo "All ${#REQUIRED[@]} mandatory gates passed for $SHA."
