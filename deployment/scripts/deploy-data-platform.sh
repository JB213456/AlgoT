#!/usr/bin/env bash
# Deploy the algot-data namespace: market data database, then live ingestion.
#
# Images are referenced by immutable digest, taken from
# deployment/manifests/arm64-digests.json (written by build-arm64-images.sh).
# Infrastructure identifiers come from Terraform outputs, so nothing is
# hard-coded and nothing containing a secret is written to disk.
set -euo pipefail

# Resolve the repository root from this script's own location, so the script
# works from any working directory. `git rev-parse` was used here originally
# and failed outright when run from outside the repo (e.g. from $HOME).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
K8S_DIR="$REPO_ROOT/kubernetes"
TF_DIR="${TF_DIR:-$REPO_ROOT/infrastructure/terraform}"
TERRAFORM="${TERRAFORM:-terraform}"
DIGESTS="${DIGEST_FILE:-$REPO_ROOT/deployment/manifests/arm64-digests.json}"
NS=algot-data
# both | database | ingestion -- lets either service be deployed and rolled
# back on its own, without disturbing the other.
COMPONENT="${COMPONENT:-both}"
case "$COMPONENT" in both|database|ingestion) ;; *) echo "FATAL: COMPONENT must be both, database or ingestion" >&2; exit 1 ;; esac
RENDER_DIR="$(mktemp -d)"
trap 'rm -rf "$RENDER_DIR"' EXIT

export OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING=True

[[ -f "$DIGESTS" ]] || { echo "FATAL: no digest file at $DIGESTS. Run build-arm64-images.sh first." >&2; exit 1; }

digest_for() {
  python3 -c "import json,sys; print(json.load(open('$DIGESTS'))['$1'])"
}

DATABASE_IMAGE="$(digest_for database)"
INGESTION_IMAGE="$(digest_for ingestion)"
TOOLBOX_IMAGE="$(digest_for toolbox)"
DATABASE_VOLUME_OCID="$("$TERRAFORM" -chdir="$TF_DIR" output -raw database_volume_id)"
OCI_NAMESPACE="$("$TERRAFORM" -chdir="$TF_DIR" output -raw objectstorage_namespace)"

# Refuse mutable tags: a :latest reference makes rollbacks meaningless and
# breaks the "immutable image digest references" requirement.
for ref in "$DATABASE_IMAGE" "$INGESTION_IMAGE" "$TOOLBOX_IMAGE"; do
  [[ "$ref" == *"@sha256:"* ]] || { echo "FATAL: $ref is not a digest reference." >&2; exit 1; }
done

# Verify every image's cosign signature before anything reaches the cluster.
#
# Fail-closed on purpose: an unsigned or mis-signed digest means the artifact
# did not come from build-arm64-images.sh or the ARM64 workflow, and this is
# the last point at which that can be caught. SKIP_SIGNATURE_VERIFY=1 exists
# only for recovering a cluster before the signing key has been created.
COSIGN_PUB="${COSIGN_PUB:-$REPO_ROOT/deployment/security/cosign.pub}"

if [[ "${SKIP_SIGNATURE_VERIFY:-0}" == "1" ]]; then
  echo "==> WARNING: skipping image signature verification (SKIP_SIGNATURE_VERIFY=1)" >&2
else
  command -v cosign >/dev/null || {
    cat >&2 <<'MSG'
FATAL: cosign is not on PATH, so image signatures cannot be verified.

Install the pinned, checksum-verified tools:

  eval "$(deployment/scripts/install-security-tools.sh --print-path)"
MSG
    exit 1
  }
  [[ -f "$COSIGN_PUB" ]] || {
    echo "FATAL: no signing public key at $COSIGN_PUB." >&2
    echo "Create the keypair once: deployment/scripts/vault-secrets.sh cosign-key" >&2
    exit 1
  }

  # Signatures live beside the images in the private repositories, so
  # verification needs a registry login. The token comes from Vault and the
  # credential goes to an isolated, mode-700 docker config that is removed
  # on exit -- never to the operator's ~/.docker/config.json.
  VERIFY_DOCKER_CONFIG="$(mktemp -d)"
  chmod 700 "$VERIFY_DOCKER_CONFIG"
  trap 'rm -rf "$RENDER_DIR" "$VERIFY_DOCKER_CONFIG"' EXIT

  VAULT_ID="$("$TERRAFORM" -chdir="$TF_DIR" output -raw vault_id)"
  REGISTRY="$("$TERRAFORM" -chdir="$TF_DIR" output -raw registry_endpoint)"
  OCIR_NS="$("$TERRAFORM" -chdir="$TF_DIR" output -raw registry_namespace)"

  TOKEN="$(oci secrets secret-bundle get-secret-bundle-by-name \
    --secret-name algot-ocir-auth-token --vault-id "$VAULT_ID" \
    --query 'data."secret-bundle-content".content' --raw-output 2>/dev/null | base64 -d || true)"
  [[ -n "$TOKEN" ]] || { echo "FATAL: no OCIR auth token in Vault; cannot read signatures." >&2; exit 1; }

  if [[ -n "${OCIR_USERNAME:-}" ]]; then
    VERIFY_USER="$OCIR_USERNAME"
  else
    USER_OCID="${OCI_CLI_USER:-$(awk -F= '/^user[[:space:]]*=/{gsub(/[[:space:]]/,"",$2); print $2; exit}' "${OCI_CLI_CONFIG_FILE:-$HOME/.oci/config}")}"
    [[ -n "$USER_OCID" ]] || { echo "FATAL: cannot determine the OCI user; set OCIR_USERNAME." >&2; exit 1; }
    VERIFY_USER="$(oci iam user get --user-id "$USER_OCID" --query 'data.name' --raw-output)"
  fi

  echo "==> Verifying image signatures"
  printf '%s' "$TOKEN" | DOCKER_CONFIG="$VERIFY_DOCKER_CONFIG" \
    docker login "$REGISTRY" -u "${OCIR_NS}/${VERIFY_USER}" --password-stdin >/dev/null
  unset TOKEN

  for ref in "$DATABASE_IMAGE" "$INGESTION_IMAGE" "$TOOLBOX_IMAGE"; do
    if DOCKER_CONFIG="$VERIFY_DOCKER_CONFIG" \
         cosign verify --key "$COSIGN_PUB" "$ref" >/dev/null 2>&1; then
      echo "    OK  ${ref##*/}"
    else
      echo "FATAL: no valid signature for $ref" >&2
      echo "Rebuild it with deployment/scripts/build-arm64-images.sh, which signs on push." >&2
      exit 1
    fi
  done
fi

echo "==> Deploying to $NS (component: $COMPONENT)"
echo "    database : $DATABASE_IMAGE"
echo "    ingestion: $INGESTION_IMAGE"
echo "    toolbox  : $TOOLBOX_IMAGE"
echo "    volume   : $DATABASE_VOLUME_OCID"

render() {
  local src="$1" dst
  dst="$RENDER_DIR/$(basename "$1")"
  sed -e "s|__DATABASE_IMAGE__|$DATABASE_IMAGE|g" \
      -e "s|__INGESTION_IMAGE__|$INGESTION_IMAGE|g" \
      -e "s|__TOOLBOX_IMAGE__|$TOOLBOX_IMAGE|g" \
      -e "s|__DATABASE_VOLUME_OCID__|$DATABASE_VOLUME_OCID|g" \
      -e "s|__OCI_NAMESPACE__|$OCI_NAMESPACE|g" \
      "$src" > "$dst"
  if grep -q '__[A-Z_]*__' "$dst"; then
    echo "FATAL: unsubstituted placeholder in $src:" >&2
    grep -o '__[A-Z_]*__' "$dst" | sort -u >&2
    exit 1
  fi
  echo "$dst"
}

apply() {
  local rendered
  rendered="$(render "$1")"
  kubectl apply -f "$rendered"
}

echo
echo "==> 1/8 Namespace, quota and limits"
kubectl apply -f "$K8S_DIR/namespaces/algot-data.yaml"

echo
echo "==> 2/8 RBAC and service accounts"
kubectl apply -f "$K8S_DIR/security/rbac.yaml"
# Caps live ingestion at one replica, so no client can double-subscribe the
# Alpaca feed. Cluster-scoped, applied before the workloads it guards.
kubectl apply -f "$K8S_DIR/security/single-instance-policy.yaml"

echo
echo "==> 3/8 Secrets (from OCI Vault)"
# Values come from OCI Vault, not from GitHub secrets or local .env files.
bash "$REPO_ROOT/deployment/scripts/vault-secrets.sh" sync

echo
echo "==> 4/8 Network policies"
kubectl apply -f "$K8S_DIR/networking/network-policies.yaml"

echo
echo "==> 5/8 Storage class, persistent volume and claim"
apply "$K8S_DIR/database/storage.yaml"

echo
echo "==> 6/8 Helper scripts"
apply "$K8S_DIR/ingestion/checkpoint-scripts.yaml"
apply "$K8S_DIR/database/maintenance-scripts.yaml"

if [[ "$COMPONENT" == "both" || "$COMPONENT" == "database" ]]; then
  echo
  echo "==> 7/8 Database"
  apply "$K8S_DIR/database/service.yaml"
  apply "$K8S_DIR/database/statefulset.yaml"

  echo "    waiting for the database to bind its volume and pass readiness..."
  if ! kubectl -n "$NS" rollout status statefulset/market-data-db --timeout=600s; then
    echo >&2
    echo "Database did not become ready. Diagnostics:" >&2
    kubectl -n "$NS" describe pod -l app.kubernetes.io/name=market-data-db | tail -40 >&2
    kubectl -n "$NS" logs -l app.kubernetes.io/name=market-data-db --tail=50 >&2 || true
    kubectl -n "$NS" get pvc,pv >&2
    exit 1
  fi

  apply "$K8S_DIR/database/backup-cronjob.yaml"
  apply "$K8S_DIR/database/compaction-cronjob.yaml"
  # Storage utilisation and reachability reporting. Part of the database
  # component because it mounts the database's volume to read its usage.
  apply "$K8S_DIR/database/monitoring-cronjob.yaml"
fi

if [[ "$COMPONENT" == "both" || "$COMPONENT" == "ingestion" ]]; then
  echo
  echo "==> 8/8 Ingestion"
  # Ingestion is useless without a reachable database, so refuse to start it
  # into a broken dependency rather than crash-looping.
  if ! kubectl -n "$NS" get statefulset market-data-db >/dev/null 2>&1; then
    echo "FATAL: the database is not deployed; deploy it before ingestion." >&2
    exit 1
  fi
  kubectl -n "$NS" rollout status statefulset/market-data-db --timeout=300s

  apply "$K8S_DIR/ingestion/deployment.yaml"

  echo "    waiting for ingestion..."
  if ! kubectl -n "$NS" rollout status deployment/data-ingestion --timeout=420s; then
    echo >&2
    echo "Ingestion did not become ready. Diagnostics:" >&2
    kubectl -n "$NS" describe pod -l app.kubernetes.io/name=data-ingestion | tail -40 >&2
    kubectl -n "$NS" logs -l app.kubernetes.io/name=data-ingestion -c live-ingestion --tail=60 >&2 || true
    exit 1
  fi
fi

echo
echo "==> Deployment summary"
kubectl -n "$NS" get statefulset,deployment,pod,svc,pvc,cronjob -o wide
echo
echo "Deployed. Verify end to end with:"
echo "  deployment/scripts/verify-deployment.sh"
