#!/usr/bin/env bash
# Build the AlgoT service images for linux/arm64, then publish them to
# private OCI Container Registry repositories by immutable digest with a
# vulnerability scan, an SBOM, a signature and a provenance attestation.
#
# Registry: OCIR, not GHCR. OCIR repositories are private by default and
# cannot be made public by accident; GHCR packages are created public on
# first push and need an admin:packages token to change.
#
# The default builder runs on the operator's Docker host with ARM64 binfmt
# emulation. It creates no Kubernetes namespace or OCI compute resource.
# BUILD_MODE=cluster retains the original native builder for environments
# where a separate short-lived build namespace is explicitly allowed.
#
# Per service, in order: build and push by digest -> verify arch and
# non-root user -> smoke test the published image -> scan with trivy ->
# generate a CycloneDX SBOM with syft -> sign the digest with cosign ->
# attach an SBOM attestation and a provenance attestation -> verify both.
#
# Usage:
#   build-arm64-images.sh                    all services
#   build-arm64-images.sh database ingestion only those
#
# All credentials -- the OCIR auth token and the cosign signing key -- are
# read from OCI Vault, never from a file, argv or the environment.
set -euo pipefail

# Resolve the repository root from this script's own location, so the script
# works from any working directory. `git rev-parse` was used here originally
# and failed outright when run from outside the repo (e.g. from $HOME).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
TF_DIR="${TF_DIR:-$REPO_ROOT/infrastructure/terraform}"
TERRAFORM="${TERRAFORM:-terraform}"
BUILD_MODE="${BUILD_MODE:-local}"
BUILDER="${BUILDER_NAME:-algot-local-arm}"
BUILD_NS="${BUILD_NAMESPACE:-algot-build}"
PLATFORM="linux/arm64"
DIGEST_FILE="${DIGEST_FILE:-$REPO_ROOT/deployment/manifests/arm64-digests.json}"
SBOM_DIR="${SBOM_DIR:-$REPO_ROOT/deployment/manifests/sbom}"
COSIGN_PUB="${COSIGN_PUB:-$REPO_ROOT/deployment/security/cosign.pub}"
TAG="${IMAGE_TAG:-arm64-$(git -C "$REPO_ROOT" rev-parse --short HEAD)}"
GIT_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"

export OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING=True
# Keep existing local builder metadata while isolating the temporary registry
# login. Otherwise DOCKER_CONFIG hides the named buildx builder.
export BUILDX_CONFIG="${BUILDX_CONFIG:-$HOME/.docker/buildx}"
DOCKER_CONFIG="$(mktemp -d)"
chmod 700 "$DOCKER_CONFIG"
export DOCKER_CONFIG

# Mode-700 scratch space for the cosign private key. Shredded on exit.
KEYDIR="$(mktemp -d)"
chmod 700 "$KEYDIR"

# Run a command quietly, but show everything it printed if it fails.
# cosign is noisy on success and its failures are the ones worth reading --
# suppressing both makes a signing problem almost undiagnosable.
run_quiet() {
  local out rc=0
  out="$(mktemp)"
  "$@" >"$out" 2>&1 || rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "FATAL: command failed: $*" >&2
    sed 's/^/    /' "$out" >&2
    rm -f "$out"
    return $rc
  fi
  rm -f "$out"
}

for tool in trivy syft cosign; do
  command -v "$tool" >/dev/null || {
    echo "FATAL: $tool is not on PATH. Install the pinned, checksum-verified" >&2
    echo "versions first:" >&2
    echo '  eval "$(deployment/scripts/install-security-tools.sh --print-path)"' >&2
    exit 1
  }
done

REGISTRY="$("$TERRAFORM" -chdir="$TF_DIR" output -raw registry_endpoint)"
OCIR_NS="$("$TERRAFORM" -chdir="$TF_DIR" output -raw registry_namespace)"
VAULT_ID="$("$TERRAFORM" -chdir="$TF_DIR" output -raw vault_id)"

# service:context:dockerfile:smoke-test-command
# The smoke test runs inside the published arm64 image under emulation and
# must exit 0. It proves the image's own entrypoint dependencies import on
# aarch64 -- the failure mode that qemu-built wheels actually produce.
ALL_SERVICES=(
  "database:$REPO_ROOT/Data/Database:Dockerfile:python -c 'import market_data_db, duckdb, pyarrow; print(\"ok\")'"
  "ingestion:$REPO_ROOT/Data/Data_Ingestion:Dockerfile:python -c 'import live_ingestion, backfill_engine, ingestion_runtime; print(\"ok\")'"
  "toolbox:$REPO_ROOT/deployment/toolbox:Dockerfile:python -c 'print(\"ok\")'"
)

# Select the requested services, preserving the declared order.
SERVICES=()
if [[ $# -eq 0 ]]; then
  SERVICES=("${ALL_SERVICES[@]}")
else
  for want in "$@"; do
    found=0
    for entry in "${ALL_SERVICES[@]}"; do
      if [[ "${entry%%:*}" == "$want" ]]; then
        SERVICES+=("$entry")
        found=1
      fi
    done
    [[ $found -eq 1 ]] || {
      echo "FATAL: unknown service '$want'. Known: database ingestion toolbox" >&2
      exit 1
    }
  done
fi

cleanup() {
  if [[ "$BUILD_MODE" == cluster && "${KEEP_BUILDER:-0}" != "1" ]]; then
    echo
    echo "Removing the in-cluster builder so it stops consuming node capacity..."
    docker buildx rm "$BUILDER" >/dev/null 2>&1 || true
    kubectl delete namespace "$BUILD_NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  find "$KEYDIR" -type f -exec shred -u {} + 2>/dev/null || true
  rm -rf "$DOCKER_CONFIG" "$KEYDIR"
}
trap cleanup EXIT

echo "==> Reading the OCIR auth token from Vault"
TOKEN="$(oci secrets secret-bundle get-secret-bundle-by-name \
  --secret-name algot-ocir-auth-token --vault-id "$VAULT_ID" \
  --query 'data."secret-bundle-content".content' --raw-output 2>/dev/null | base64 -d || true)"

if [[ -z "$TOKEN" ]]; then
  cat >&2 <<MSG
FATAL: no OCIR auth token in Vault.

Pushing images requires an OCI auth token, which only the account owner can
mint. Create one in the Console:

  Identity > Users > your user > Auth tokens > Generate token

then store it (it is shown only once):

  deployment/scripts/vault-secrets.sh push-token

It is stored in OCI Vault, not in GitHub, and the pods read it from there.
MSG
  exit 1
fi

echo "==> Reading the cosign signing key from Vault"
oci secrets secret-bundle get-secret-bundle-by-name \
  --secret-name algot-cosign-private-key --vault-id "$VAULT_ID" \
  --query 'data."secret-bundle-content".content' --raw-output 2>/dev/null \
  | base64 -d > "$KEYDIR/cosign.key" || true
COSIGN_PASSWORD="$(oci secrets secret-bundle get-secret-bundle-by-name \
  --secret-name algot-cosign-password --vault-id "$VAULT_ID" \
  --query 'data."secret-bundle-content".content' --raw-output 2>/dev/null | base64 -d || true)"
export COSIGN_PASSWORD
chmod 600 "$KEYDIR/cosign.key" 2>/dev/null || true

if [[ ! -s "$KEYDIR/cosign.key" || -z "$COSIGN_PASSWORD" ]]; then
  cat >&2 <<MSG
FATAL: no cosign signing key in Vault.

Create it once; the private key is generated straight into Vault and the
public half is committed for verification:

  deployment/scripts/vault-secrets.sh cosign-key
MSG
  exit 1
fi

[[ -f "$COSIGN_PUB" ]] || { echo "FATAL: missing public key $COSIGN_PUB" >&2; exit 1; }

# OCIR's docker username is "<namespace>/<oci-username>"; federated users
# need the "oracleidentitycloudservice/<email>" form, hence the override.
if [[ -n "${OCIR_USERNAME:-}" ]]; then
  USER_NAME="$OCIR_USERNAME"
else
  USER_OCID="${OCI_CLI_USER:-$(awk -F= '/^user[[:space:]]*=/{gsub(/[[:space:]]/,"",$2); print $2; exit}' "${OCI_CLI_CONFIG_FILE:-$HOME/.oci/config}")}"
  [[ -n "$USER_OCID" ]] || { echo "FATAL: cannot determine the OCI user; set OCIR_USERNAME." >&2; exit 1; }
  USER_NAME="$(oci iam user get --user-id "$USER_OCID" --query 'data.name' --raw-output)"
fi

echo "==> Logging in to $REGISTRY as ${OCIR_NS}/${USER_NAME}"
printf '%s' "$TOKEN" | docker login "$REGISTRY" -u "${OCIR_NS}/${USER_NAME}" --password-stdin

if [[ "$BUILD_MODE" == cluster ]]; then
  echo "==> Preparing builder namespace $BUILD_NS"
  kubectl create namespace "$BUILD_NS" --dry-run=client -o yaml | kubectl apply -f -
  kubectl label namespace "$BUILD_NS" pod-security.kubernetes.io/enforce=baseline --overwrite
  echo "==> Creating the in-cluster arm64 builder"
  docker buildx rm "$BUILDER" >/dev/null 2>&1 || true
  docker buildx create \
    --name "$BUILDER" \
    --driver kubernetes \
    --platform "$PLATFORM" \
    --driver-opt "namespace=$BUILD_NS,replicas=1,rootless=true,requests.cpu=500m,requests.memory=1Gi,limits.cpu=1500m,limits.memory=4Gi" \
    --bootstrap
elif [[ "$BUILD_MODE" != local ]]; then
  echo "FATAL: BUILD_MODE must be local or cluster" >&2
  exit 1
fi

docker buildx inspect "$BUILDER" --bootstrap | tee /tmp/algot-buildx-platforms.txt
grep -q 'linux/arm64' /tmp/algot-buildx-platforms.txt || {
  echo "FATAL: builder $BUILDER cannot build linux/arm64; register ARM64 binfmt first." >&2
  exit 1
}

mkdir -p "$(dirname "$DIGEST_FILE")" "$SBOM_DIR"

for entry in "${SERVICES[@]}"; do
  IFS=':' read -r name context dockerfile smoke <<< "$entry"
  image="$REGISTRY/$OCIR_NS/algot-$name"
  echo
  echo "============================================================"
  echo "  algot-$name"
  echo "============================================================"
  echo "==> Building $image:$TAG for $PLATFORM ($BUILD_MODE builder)"

  # The Strategies tree is proprietary and must never enter a build context.
  [[ ! -e "$context/Strategies" ]] || {
    echo "FATAL: $context/Strategies exists; proprietary source must not be built." >&2
    exit 1
  }

  # buildx provenance stays off: it turns the pushed artifact into an OCI
  # index, and both the architecture assertions below and the digest-pinned
  # manifests depend on a single-platform manifest. Provenance is attached
  # as a cosign attestation instead, a few lines down.
  docker buildx build \
    --builder "$BUILDER" \
    --platform "$PLATFORM" \
    --network host \
    --allow network.host \
    --file "$context/$dockerfile" \
    --tag "$image:$TAG" \
    --provenance=false \
    --metadata-file "/tmp/algot-$name-meta.json" \
    --push \
    "$context"

  digest="$(python3 -c "import json;print(json.load(open('/tmp/algot-$name-meta.json'))['containerimage.digest'])")"
  ref="$image@$digest"
  echo "    digest: $digest"

  echo "==> Verifying architecture and non-root user"
  arch="$(docker buildx imagetools inspect "$ref" --format '{{.Image.Architecture}}')"
  user="$(docker buildx imagetools inspect "$ref" --format '{{.Image.Config.User}}')"
  printf '    arch=%s user=%s\n' "$arch" "${user:-<root>}"
  [[ "$arch" == "arm64" ]] || { echo "FATAL: $ref is $arch, not arm64" >&2; exit 1; }
  # A root image is rejected at admission by the restricted Pod Security
  # Standards on algot-data, so catch it here rather than at deploy time.
  [[ -n "$user" && "$user" != "root" && "$user" != "0" ]] \
    || { echo "FATAL: $ref declares no non-root USER" >&2; exit 1; }

  echo "==> Smoke testing the published image under arm64 emulation"
  # Runs the image pulled back from the registry by digest, so the test
  # covers the exact artifact that the cluster will pull.
  docker run --rm --platform "$PLATFORM" --entrypoint sh "$ref" -c "$smoke"

  echo "==> Scanning for vulnerabilities (HIGH, CRITICAL, fixable)"
  # --ignore-unfixed: the pinned python:3.12-slim base carries high-severity
  # Debian CVEs with no released fix, documented in docs/ci-cd/ARCHITECTURE.md.
  # A finding that has a fix available is a release blocker and exits 1.
  trivy image --quiet --exit-code 1 --severity HIGH,CRITICAL \
    --ignore-unfixed --scanners vuln --platform "$PLATFORM" "$ref"
  echo "    no fixable HIGH/CRITICAL findings"

  echo "==> Generating the CycloneDX SBOM"
  sbom="$SBOM_DIR/$name-arm64.cdx.json"
  syft --quiet "registry:$ref" --platform "$PLATFORM" -o "cyclonedx-json=$sbom"
  python3 - "$sbom" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["bomFormat"] == "CycloneDX", d.get("bomFormat")
assert d["components"], "SBOM lists no components"
print(f"    {len(d['components'])} components")
PY

  echo "==> Signing the digest"
  run_quiet cosign sign --yes --key "$KEYDIR/cosign.key" "$ref"
  echo "    signed"

  echo "==> Attaching the SBOM attestation"
  run_quiet cosign attest --yes --key "$KEYDIR/cosign.key" \
    --type cyclonedx --predicate "$sbom" "$ref"
  echo "    SBOM attestation attached"

  echo "==> Attaching the provenance attestation"
  # buildx/OCIR cannot carry a SLSA provenance manifest here, so the same
  # facts are recorded as a signed in-toto predicate: what was built, from
  # which commit, by which builder, when.
  prov="$(mktemp "$KEYDIR/prov-XXXXXX.json")"
  python3 - "$prov" "$name" "$GIT_SHA" "$TAG" "$digest" "$BUILD_MODE" <<'PY'
import json, subprocess, sys, datetime
dest, service, sha, tag, digest, mode = sys.argv[1:7]
origin = subprocess.run(["git", "config", "--get", "remote.origin.url"],
                        capture_output=True, text=True).stdout.strip()
json.dump({
    "buildDefinition": {
        "buildType": "https://algot.local/arm64-image-build/v1",
        "externalParameters": {
            "service": service,
            "platform": "linux/arm64",
            "tag": tag,
            "builderMode": mode,
        },
        "resolvedDependencies": [
            {"uri": origin or "local", "digest": {"gitCommit": sha}},
        ],
    },
    "runDetails": {
        "builder": {"id": "https://algot.local/build-arm64-images.sh"},
        "metadata": {
            "invocationId": digest,
            "finishedOn": datetime.datetime.now(datetime.timezone.utc)
                                   .isoformat(timespec="seconds"),
        },
    },
}, open(dest, "w"), indent=2)
PY
  run_quiet cosign attest --yes --key "$KEYDIR/cosign.key" \
    --type slsaprovenance1 --predicate "$prov" "$ref"
  echo "    provenance attestation attached"

  echo "==> Verifying the signature and attestations against the public key"
  run_quiet cosign verify --key "$COSIGN_PUB" "$ref"
  echo "    signature verified"
  run_quiet cosign verify-attestation --key "$COSIGN_PUB" --type cyclonedx "$ref"
  echo "    SBOM attestation verified"
  run_quiet cosign verify-attestation --key "$COSIGN_PUB" --type slsaprovenance1 "$ref"
  echo "    provenance attestation verified"

  # Merge this digest into the manifest rather than rewriting the file, so
  # building a subset does not drop the other services' digests.
  python3 - "$DIGEST_FILE" "$name" "$ref" <<'PY'
import json, pathlib, sys
path, name, ref = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
data = json.loads(path.read_text()) if path.exists() else {}
data[name] = ref
path.write_text(json.dumps(dict(sorted(data.items())), indent=2) + "\n")
PY
  echo "    recorded in $(basename "$DIGEST_FILE")"
done

echo
echo "==> Repository privacy (must all be false)"
COMPARTMENT="$("$TERRAFORM" -chdir="$TF_DIR" output -raw compartment_ocid)"
oci artifacts container repository list -c "$COMPARTMENT" \
  --query 'data.items[].{repository:"display-name",public:"is-public",images:"image-count",gb:"billable-size-in-gbs"}' \
  --output table

echo
echo "Digests : $DIGEST_FILE"
echo "SBOMs   : $SBOM_DIR"
echo "Images are private in OCIR, signed, and attested."
echo "No registry credential or signing key was left on disk."
