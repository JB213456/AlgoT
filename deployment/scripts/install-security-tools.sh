#!/usr/bin/env bash
# Install checksum-verified container security tooling: trivy (vulnerability
# scanning), syft (SBOM generation) and cosign (signing and attestation).
#
# Works both on a GitHub runner and on an operator workstation. On a runner
# RUNNER_TEMP is already set and the tool directory is appended to GITHUB_PATH;
# locally it defaults to a per-user cache directory and the caller adds the
# printed directory to PATH itself:
#
#     eval "$(deployment/scripts/install-security-tools.sh --print-path)"
#
# Every download is pinned to an exact version and verified against a
# hard-coded SHA-256 before it is unpacked or made executable.
set -euo pipefail

TRIVY_VERSION=0.76.2
SYFT_VERSION=1.38.0
COSIGN_VERSION=3.0.3

PRINT_PATH=0
[[ "${1:-}" == "--print-path" ]] && PRINT_PATH=1

# Both architectures are needed: operator workstations are x86_64, while the
# ARM64 image workflow runs on an aarch64 runner. Checksums are taken from
# each release's signed checksums file and hard-coded here, so the installer
# never trusts a checksum fetched at run time.
case "$(uname -m)" in
  x86_64)
    TRIVY_ASSET="trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz"
    TRIVY_SHA256=bc0fc726e017b8927a62b2d3531d7a84ee01769daf5c99ed866dced54089fdbf
    SYFT_ASSET="syft_${SYFT_VERSION}_linux_amd64.tar.gz"
    SYFT_SHA256=bc664debb00db1b9b4b9dae4ba1a29d99621e33b641b94b78b9048374b812a3d
    COSIGN_ASSET="cosign-linux-amd64"
    COSIGN_SHA256=052363a0e23e2e7ed53641351b8b420918e7e08f9c1d8a42a3dd3877a78a2e10
    ;;
  aarch64|arm64)
    TRIVY_ASSET="trivy_${TRIVY_VERSION}_Linux-ARM64.tar.gz"
    TRIVY_SHA256=ab9d51f86e6a5fb936f605faff6b593ac51bc378fff4d3e8b9d58358b9c367c3
    SYFT_ASSET="syft_${SYFT_VERSION}_linux_arm64.tar.gz"
    SYFT_SHA256=5e28d8e86592a0f269c3bab153de2901714382366ed783b26774a5146361e33a
    COSIGN_ASSET="cosign-linux-arm64"
    COSIGN_SHA256=81398231362031e3c7afd6a7508c57049460cd7e02736f1ebe89a452102253e5
    ;;
  *)
    echo "FATAL: no pinned tool builds for $(uname -m)." >&2
    exit 1
    ;;
esac

# On a runner RUNNER_TEMP is always set. Locally fall back to a cache
# directory so the script does not need a CI environment to be useful.
WORK="${RUNNER_TEMP:-${XDG_CACHE_HOME:-$HOME/.cache}/algot}"
TOOLS="$WORK/algot-tools"
mkdir -p "$TOOLS"

# On x86_64 these binaries inspect arm64 images, which is fine: trivy and
# syft read the registry manifest and layers rather than executing the
# image, and cosign only ever signs a digest.

log() { [[ "$PRINT_PATH" -eq 1 ]] && return 0; echo "$@"; }

fetch_verified() {
  # fetch_verified <url> <destination> <sha256>
  local url="$1" dest="$2" want="$3"
  curl -fsSL --retry 3 --retry-delay 2 "$url" -o "$dest"
  echo "$want  $dest" | sha256sum -c - >/dev/null
}

if [[ ! -x "$TOOLS/trivy" ]]; then
  log "==> trivy $TRIVY_VERSION"
  fetch_verified \
    "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/${TRIVY_ASSET}" \
    "$WORK/algot-trivy.tar.gz" "$TRIVY_SHA256"
  tar -xzf "$WORK/algot-trivy.tar.gz" -C "$TOOLS" trivy
fi

if [[ ! -x "$TOOLS/syft" ]]; then
  log "==> syft $SYFT_VERSION"
  fetch_verified \
    "https://github.com/anchore/syft/releases/download/v${SYFT_VERSION}/${SYFT_ASSET}" \
    "$WORK/algot-syft.tar.gz" "$SYFT_SHA256"
  tar -xzf "$WORK/algot-syft.tar.gz" -C "$TOOLS" syft
fi

if [[ ! -x "$TOOLS/cosign" ]]; then
  log "==> cosign $COSIGN_VERSION"
  fetch_verified \
    "https://github.com/sigstore/cosign/releases/download/v${COSIGN_VERSION}/${COSIGN_ASSET}" \
    "$TOOLS/cosign" "$COSIGN_SHA256"
  chmod +x "$TOOLS/cosign"
fi

if [[ -n "${GITHUB_PATH:-}" ]]; then
  printf '%s\n' "$TOOLS" >> "$GITHUB_PATH"
fi

if [[ "$PRINT_PATH" -eq 1 ]]; then
  printf 'export PATH=%q:"$PATH"\n' "$TOOLS"
else
  echo
  echo "Installed to $TOOLS"
  "$TOOLS/trivy" --version | head -1
  "$TOOLS/syft" version | head -2 | tail -1
  "$TOOLS/cosign" version 2>&1 | grep -i 'GitVersion' || true
fi
