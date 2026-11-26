#!/usr/bin/env bash
# Render the Kubernetes manifests with syntactically valid stand-ins for the
# deploy-time placeholders, so static analysers see well-formed YAML.
#
# The real values come from Terraform outputs and the digest manifest at
# deploy time (deploy-data-platform.sh). Analysers that run on untrusted pull
# requests have neither, and `image: __DATABASE_IMAGE__` is not a valid image
# reference, so schema and policy scanners would report spurious findings.
#
# The stand-ins are deliberately obvious: an all-zero digest and a
# "placeholderns" registry namespace. Nothing here is a credential, and
# nothing rendered here is ever applied to a cluster.
#
# Usage: render-placeholders.sh <output-dir> [<manifest-dir>]
set -euo pipefail

OUT_DIR="${1:?usage: render-placeholders.sh <output-dir> [<manifest-dir>]}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
SRC_DIR="${2:-$REPO_ROOT/kubernetes}"

ZERO=0000000000000000000000000000000000000000000000000000000000000000
REG=lhr.ocir.io/placeholderns

mkdir -p "$OUT_DIR"

count=0
while IFS= read -r -d '' f; do
  rel="${f#"$SRC_DIR"/}"
  # Flatten the tree: scanners are pointed at one directory, and the
  # original path is preserved in the filename so a finding is traceable.
  dst="$OUT_DIR/$(printf '%s' "$rel" | tr '/' '_')"
  sed -e "s|__DATABASE_IMAGE__|$REG/algot-database@sha256:$ZERO|g" \
      -e "s|__INGESTION_IMAGE__|$REG/algot-ingestion@sha256:$ZERO|g" \
      -e "s|__TOOLBOX_IMAGE__|$REG/algot-toolbox@sha256:$ZERO|g" \
      -e 's|__DATABASE_VOLUME_OCID__|ocid1.volume.oc1.uk-london-1.placeholder|g' \
      -e 's|__OCI_NAMESPACE__|placeholderns|g' \
      -e 's|__RUN_ID__|render-check|g' \
      -e 's|__ARCHIVE__|render-check.tar.zst|g' \
      -e 's|__TEST_PROVIDER__|test-provider|g' \
      -e 's|__BASELINE_BARS__|0|g' \
      -e 's|__BASELINE_QUOTES__|0|g' \
      -e 's|__BASELINE_TRADES__|0|g' \
      -e 's|__EXPECTED_PROVIDER_ROWS__|0|g' \
      "$f" > "$dst"
  count=$((count + 1))
# Dotfiles alongside the manifests are tool configuration
# (kubernetes/.checkov.yaml), never Kubernetes objects: rendering them would
# feed a scanner its own config as if it were a manifest.
done < <(find "$SRC_DIR" -name '*.yaml' -type f ! -name '.*' -print0)

# A silent zero-file render would make every downstream scanner pass
# vacuously, which is the one outcome that must not look like success.
if [[ "$count" -eq 0 ]]; then
  echo "FATAL: no manifests found under $SRC_DIR" >&2
  exit 1
fi

# Any placeholder left in a live field is one this script does not know
# about: fail rather than hand a scanner YAML with an invalid field value.
# Comment lines are excluded -- the manifests document their own
# placeholders in comments, and that prose is not a field value.
LEFTOVER="$(grep -rhv '^[[:space:]]*#' "$OUT_DIR" \
  | grep -o '__[A-Z_]\{2,\}__' | sort -u || true)"
if [[ -n "$LEFTOVER" ]]; then
  echo "FATAL: unrendered placeholders remain; add them to $0:" >&2
  printf '%s\n' "$LEFTOVER" >&2
  exit 1
fi

echo "Rendered $count manifest file(s) from $SRC_DIR into $OUT_DIR"
