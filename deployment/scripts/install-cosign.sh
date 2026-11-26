#!/usr/bin/env bash
set -euo pipefail
: "${RUNNER_TEMP:?RUNNER_TEMP must be set}"
mkdir -p "$RUNNER_TEMP/algot-tools"
curl -fsSL https://github.com/sigstore/cosign/releases/download/v3.0.3/cosign-linux-amd64 -o "$RUNNER_TEMP/algot-tools/cosign"
echo "052363a0e23e2e7ed53641351b8b420918e7e08f9c1d8a42a3dd3877a78a2e10  $RUNNER_TEMP/algot-tools/cosign" | sha256sum -c -
chmod +x "$RUNNER_TEMP/algot-tools/cosign"
printf '%s\n' "$RUNNER_TEMP/algot-tools" >> "$GITHUB_PATH"
