#!/usr/bin/env bash
# Report and reclaim OCI Container Registry storage for the AlgoT images.
#
# Why this exists: every rebuild of a service pushes a new manifest and
# leaves the previous one in place. When a tag such as arm64-<sha> moves to
# the new manifest the old one becomes untagged but is still stored and
# still billable. Nothing in OCIR expires it, so without this the
# repositories grow without bound.
#
# Container Image Storage is not enumerated on Oracle's Always Free
# resources page, so this deployment keeps usage far below the point where
# any charge could appear and asserts a hard budget here. The script exits
# non-zero if total usage exceeds STORAGE_BUDGET_GB.
#
#   registry-gc.sh            report what would be removed (default, read-only)
#   registry-gc.sh --apply    actually delete the orphaned manifests
#
# Safety: a manifest is only ever a deletion candidate when it is untagged
# AND not referenced by deployment/manifests/arm64-digests.json AND not in
# use by a running pod AND not referenced by a retained cosign artifact.
#
# That last condition is essential and easy to get wrong. cosign 3.x stores
# a signature and its attestations as OCI referrers: a tagged fallback index
# at sha256-<subject-digest> whose entries are *untagged* manifests. Deleting
# untagged manifests without reading those indexes destroys the signatures
# while leaving the index behind, so verification then fails against an
# image that is otherwise intact. This script therefore reads every retained
# index and protects the manifests it points at.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
TF_DIR="${TF_DIR:-$REPO_ROOT/infrastructure/terraform}"
TERRAFORM="${TERRAFORM:-terraform}"
DIGEST_FILE="${DIGEST_FILE:-$REPO_ROOT/deployment/manifests/arm64-digests.json}"
STORAGE_BUDGET_GB="${STORAGE_BUDGET_GB:-5}"
NS="${NS:-algot-data}"

APPLY=0
case "${1:-}" in
  --apply) APPLY=1 ;;
  ""|--dry-run) ;;
  *) sed -n '2,20p' "$0" >&2; exit 2 ;;
esac

export OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING=True

COMPARTMENT="$("$TERRAFORM" -chdir="$TF_DIR" output -raw compartment_ocid)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> Reading repository storage"
oci artifacts container repository list -c "$COMPARTMENT" --all > "$WORK/repos.json"

echo "==> Reading image manifests"
oci artifacts container image list -c "$COMPARTMENT" --all > "$WORK/images.json"

# Digests that must never be deleted: the recorded deployment digests plus
# anything a running pod is actually using. The in-cluster lookup is
# best-effort -- if the API is unreachable the recorded digests still apply,
# and the script says so rather than silently protecting less.
: > "$WORK/protected.txt"
if [[ -f "$DIGEST_FILE" ]]; then
  python3 - "$DIGEST_FILE" <<'PY' >> "$WORK/protected.txt"
import json, pathlib, sys
for ref in json.loads(pathlib.Path(sys.argv[1]).read_text()).values():
    if "@" in ref:
        print(ref.split("@", 1)[1])
PY
fi

CLUSTER_SEEN=0
if kubectl get ns "$NS" --request-timeout=15s >/dev/null 2>&1; then
  kubectl get pods -n "$NS" -o json --request-timeout=30s 2>/dev/null \
    | python3 -c '
import json, sys
d = json.load(sys.stdin)
for p in d.get("items", []):
    st = p.get("status", {})
    for key in ("containerStatuses", "initContainerStatuses", "ephemeralContainerStatuses"):
        for cs in st.get(key) or []:
            iid = cs.get("imageID") or ""
            if "@" in iid:
                print(iid.split("@", 1)[1])
' >> "$WORK/protected.txt" && CLUSTER_SEEN=1
fi

sort -u "$WORK/protected.txt" -o "$WORK/protected.txt"

# Protect the manifests that retained cosign artifacts point at.
#
# Reading a referrers index needs registry access, so this logs in with the
# Vault-held token into an isolated, mode-700 docker config that is removed
# on exit. If the registry cannot be read the script stops rather than
# guessing: deleting untagged manifests blind is what destroys signatures.
GC_DOCKER_CONFIG="$(mktemp -d)"
chmod 700 "$GC_DOCKER_CONFIG"
trap 'rm -rf "$WORK" "$GC_DOCKER_CONFIG"' EXIT

REGISTRY="$("$TERRAFORM" -chdir="$TF_DIR" output -raw registry_endpoint)"
OCIR_NS="$("$TERRAFORM" -chdir="$TF_DIR" output -raw registry_namespace)"
VAULT_ID="$("$TERRAFORM" -chdir="$TF_DIR" output -raw vault_id)"

TOKEN="$(oci secrets secret-bundle get-secret-bundle-by-name \
  --secret-name algot-ocir-auth-token --vault-id "$VAULT_ID" \
  --query 'data."secret-bundle-content".content' --raw-output 2>/dev/null | base64 -d || true)"
[[ -n "$TOKEN" ]] || { echo "FATAL: no OCIR auth token in Vault; cannot read referrers safely." >&2; exit 1; }

if [[ -n "${OCIR_USERNAME:-}" ]]; then
  GC_USER="$OCIR_USERNAME"
else
  USER_OCID="${OCI_CLI_USER:-$(awk -F= '/^user[[:space:]]*=/{gsub(/[[:space:]]/,"",$2); print $2; exit}' "${OCI_CLI_CONFIG_FILE:-$HOME/.oci/config}")}"
  [[ -n "$USER_OCID" ]] || { echo "FATAL: cannot determine the OCI user; set OCIR_USERNAME." >&2; exit 1; }
  GC_USER="$(oci iam user get --user-id "$USER_OCID" --query 'data.name' --raw-output)"
fi

printf '%s' "$TOKEN" | DOCKER_CONFIG="$GC_DOCKER_CONFIG" \
  docker login "$REGISTRY" -u "${OCIR_NS}/${GC_USER}" --password-stdin >/dev/null 2>&1 \
  || { echo "FATAL: could not log in to $REGISTRY to read referrers." >&2; exit 1; }
unset TOKEN

echo "==> Reading cosign referrer indexes"
# The cosign fallback tags worth reading, as "<repo> <tag>".
#
# Only indexes whose subject image is itself retained are read. An index for
# an image that has already gone is reclaimable along with everything it
# points at, so protecting its referrers would leak them permanently.
python3 - "$WORK/images.json" "$WORK/protected.txt" <<'PY' > "$WORK/cosign-tags.txt"
import json, pathlib, sys
items = json.loads(pathlib.Path(sys.argv[1]).read_text())["data"]["items"]
protected = {l.strip() for l in
             pathlib.Path(sys.argv[2]).read_text().splitlines() if l.strip()}
seen = set()
for i in items:
    repo, tag = i.get("repository-name") or "", i.get("version") or ""
    if not repo.startswith("algot-") or not tag.startswith("sha256-"):
        continue
    stem = tag[len("sha256-"):].partition(".")[0]
    if len(stem) != 64 or not all(c in "0123456789abcdef" for c in stem):
        continue
    if f"sha256:{stem}" not in protected:
        continue
    if (repo, tag) not in seen:
        seen.add((repo, tag))
        print(repo, tag)
PY

REFERRER_COUNT=0
while read -r repo tag; do
  [[ -n "$repo" ]] || continue
  raw="$(DOCKER_CONFIG="$GC_DOCKER_CONFIG" docker buildx imagetools inspect \
    --raw "$REGISTRY/$OCIR_NS/$repo:$tag" 2>/dev/null || true)"
  [[ -n "$raw" ]] || { echo "FATAL: could not read the referrers index $repo:$tag." >&2; exit 1; }
  # Protect both the index's own subject and every manifest it lists.
  n="$(printf '%s' "$raw" | python3 -c '
import json, sys
d = json.load(sys.stdin)
out = [m["digest"] for m in d.get("manifests", []) if m.get("digest")]
print("\n".join(out))
' | tee -a "$WORK/protected.txt" | grep -c . || true)"
  REFERRER_COUNT=$((REFERRER_COUNT + n))
done < "$WORK/cosign-tags.txt"

sort -u "$WORK/protected.txt" -o "$WORK/protected.txt"
echo "    protected $REFERRER_COUNT manifest(s) referenced by cosign indexes"

if [[ "$CLUSTER_SEEN" -eq 1 ]]; then
  echo "    protected digests: $(wc -l < "$WORK/protected.txt") (recorded + in-use)"
else
  echo "    protected digests: $(wc -l < "$WORK/protected.txt") (recorded only --"
  echo "    the Kubernetes API was unreachable, so in-use images were not consulted)"
fi

python3 - "$WORK/repos.json" "$WORK/images.json" "$WORK/protected.txt" \
         "$WORK/delete.txt" "$STORAGE_BUDGET_GB" <<'PY'
import json, pathlib, sys
from collections import defaultdict

repos_path, images_path, protected_path, delete_path, budget = sys.argv[1:6]
budget = float(budget)

repos = json.loads(pathlib.Path(repos_path).read_text())["data"]["items"]
images = json.loads(pathlib.Path(images_path).read_text())["data"]["items"]
protected = {line.strip() for line in
             pathlib.Path(protected_path).read_text().splitlines() if line.strip()}

repos = [r for r in repos if (r.get("display-name") or "").startswith("algot-")]
images = [i for i in images
          if (i.get("repository-name") or "").startswith("algot-")
          and i.get("lifecycle-state") == "AVAILABLE"]

# Group tags per (repo, digest): the API returns one row per tag.
tags = defaultdict(set)
meta = {}
for i in images:
    key = (i["repository-name"], i["digest"])
    if i.get("version"):
        tags[key].add(i["version"])
    meta.setdefault(key, i)

# A cosign artifact's tag encodes the digest of the image it refers to.
#
# Two layouts appear in practice and both must be recognised:
#   sha256-<hex>.sig / .att   cosign's older per-kind tags
#   sha256-<hex>              the OCI 1.1 referrers fallback tag, which is
#                             what cosign 3.x writes here -- one index
#                             holding the signature and every attestation
# Anything else is a normal image tag.
def cosign_subject(tag):
    if not tag.startswith("sha256-"):
        return None
    rest = tag[len("sha256-"):]
    stem, _, suffix = rest.partition(".")
    if suffix and suffix not in ("sig", "att", "sbom"):
        return None
    if len(stem) != 64 or not all(c in "0123456789abcdef" for c in stem):
        return None
    return "sha256:" + stem

live_digests = {d for (_r, d) in meta}

delete, keep = [], []
for key, i in sorted(meta.items()):
    repo, digest = key
    tagset = tags[key]
    subjects = {cosign_subject(t) for t in tagset} - {None}

    if digest in protected:
        keep.append((repo, digest, tagset,
                     "deployed, in use, or cosign signature data"))
    elif subjects:
        # Retain a signature only while its subject image still exists.
        if subjects & (protected | live_digests):
            keep.append((repo, digest, tagset, "cosign artifact for a retained image"))
        else:
            delete.append((repo, digest, tagset, i["id"], "cosign artifact, subject gone"))
    elif not tagset:
        delete.append((repo, digest, tagset, i["id"], "untagged, superseded by a newer build"))
    else:
        keep.append((repo, digest, tagset, "tagged"))

print()
print(f"{'REPOSITORY':20s} {'PUBLIC':7s} {'IMAGES':>7s} {'GB':>8s}")
print("-" * 46)
total = 0.0
public = []
for r in sorted(repos, key=lambda x: x["display-name"]):
    gb = float(r.get("billable-size-in-gbs") or 0)
    total += gb
    if r.get("is-public"):
        public.append(r["display-name"])
    print(f"{r['display-name']:20s} {str(r.get('is-public')):7s} "
          f"{str(r.get('image-count')):>7s} {gb:>8.3f}")
print("-" * 46)
print(f"{'TOTAL':20s} {'':7s} {'':>7s} {total:>8.3f}")

print()
print(f"Retained ({len(keep)}):")
for repo, digest, tagset, why in keep:
    print(f"  {repo:18s} {digest[:26]}...  {','.join(sorted(tagset)) or '<untagged>':28s} {why}")

print()
print(f"Reclaimable ({len(delete)}):")
if not delete:
    print("  nothing to reclaim")
for repo, digest, tagset, _id, why in delete:
    print(f"  {repo:18s} {digest[:26]}...  {','.join(sorted(tagset)) or '<untagged>':28s} {why}")

pathlib.Path(delete_path).write_text(
    "".join(f"{i}\t{r}\t{d}\n" for r, d, _t, i, _w in delete))

print()
if public:
    sys.exit(f"ERROR: these repositories are public, but AlgoT images must be "
             f"private: {', '.join(public)}")
if total > budget:
    sys.exit(f"ERROR: total image storage {total:.3f} GB exceeds the "
             f"{budget:.1f} GB budget. Reclaim space before pushing more.")
print(f"Storage {total:.3f} GB is within the {budget:.1f} GB budget.")
PY

COUNT="$(wc -l < "$WORK/delete.txt" | tr -d ' ')"

if [[ "$COUNT" -eq 0 ]]; then
  echo
  echo "Nothing to delete."
  exit 0
fi

if [[ "$APPLY" -ne 1 ]]; then
  echo
  echo "Read-only report. Re-run with --apply to delete the $COUNT manifest(s) above."
  exit 0
fi

echo
echo "==> Deleting $COUNT orphaned manifest(s)"
while IFS=$'\t' read -r image_id repo digest; do
  [[ -n "$image_id" ]] || continue
  echo "    $repo ${digest:0:26}..."
  oci artifacts container image delete --image-id "$image_id" --force >/dev/null
done < "$WORK/delete.txt"

echo
echo "==> Storage after reclamation"
oci artifacts container repository list -c "$COMPARTMENT" \
  --query 'data.items[].{repository:"display-name",public:"is-public",images:"image-count",gb:"billable-size-in-gbs"}' \
  --output table
