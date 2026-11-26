#!/usr/bin/env bash
# Manage AlgoT application secrets in OCI Vault.
#
# Secrets live in Oracle, not in GitHub. Nothing here is ever committed, and
# values are never echoed.
#
#   vault-secrets.sh push            seed/rotate Vault from the local .env files
#   vault-secrets.sh push-token      store an OCIR auth token (read from stdin)
#   vault-secrets.sh cosign-key      generate the image signing keypair into Vault
#   vault-secrets.sh sync            create the Kubernetes Secrets from Vault
#   vault-secrets.sh list            list secret names and versions (no values)
#
# Terraform owns the vault and its master key; it deliberately does NOT own
# the secret values, because an oci_vault_secret resource records its content
# in Terraform state in plain text.
set -euo pipefail

# Resolve the repository root from this script's own location, so the script
# works from any working directory. `git rev-parse` was used here originally
# and failed outright when run from outside the repo (e.g. from $HOME).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
TF_DIR="${TF_DIR:-$REPO_ROOT/infrastructure/terraform}"
TERRAFORM="${TERRAFORM:-terraform}"
NS=algot-data
DB_ENV="${DB_ENV:-$REPO_ROOT/Data/Database/.env}"
ING_ENV="${ING_ENV:-$REPO_ROOT/Data/Data_Ingestion/.env}"

export OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING=True

COMPARTMENT="$("$TERRAFORM" -chdir="$TF_DIR" output -raw compartment_ocid)"
VAULT_ID="$("$TERRAFORM" -chdir="$TF_DIR" output -raw vault_id)"
KEY_ID="$("$TERRAFORM" -chdir="$TF_DIR" output -raw vault_key_id)"

# Vault secret name -> where its value comes from in the local .env files.
# Only genuine credentials are stored here; non-secret configuration such as
# ALPACA_SYMBOLS is plain environment in the Deployment manifest.
secret_source() {
  case "$1" in
    algot-market-data-db-api-keys)  echo "$DB_ENV:MARKET_DATA_DB_API_KEYS" ;;
    algot-market-data-db-client-key) echo "$ING_ENV:MARKET_DATA_DB_API_KEY" ;;
    algot-alpaca-api-key)           echo "$ING_ENV:ALPACA_API_KEY" ;;
    algot-alpaca-secret-key)        echo "$ING_ENV:ALPACA_SECRET_KEY" ;;
    algot-lse-api-key)              echo "$ING_ENV:LSE_API_KEY" ;;
    *) return 1 ;;
  esac
}

ALL_SECRETS=(
  algot-market-data-db-api-keys
  algot-market-data-db-client-key
  algot-alpaca-api-key
  algot-alpaca-secret-key
  algot-lse-api-key
)

env_get() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 1
  python3 - "$file" "$key" <<'PY'
import sys, pathlib
path, key = sys.argv[1], sys.argv[2]
for line in pathlib.Path(path).read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    if k.strip() == key:
        print(v.strip().strip('"').strip("'"))
        break
PY
}

secret_ocid() {
  oci vault secret list --compartment-id "$COMPARTMENT" --vault-id "$VAULT_ID" \
    --name "$1" --all \
    --query "data[?\"lifecycle-state\"!='DELETED']|[0].id" --raw-output 2>/dev/null || true
}

# Writes a value to Vault, creating the secret or adding a new version.
put_secret() {
  local name="$1" value="$2" b64 existing
  b64="$(printf '%s' "$value" | base64 -w0)"
  existing="$(secret_ocid "$name")"

  if [[ -n "$existing" && "$existing" != "null" ]]; then
    oci vault secret update-base64 \
      --secret-id "$existing" \
      --secret-content-content "$b64" \
      --force >/dev/null
    echo "    rotated  $name"
  else
    oci vault secret create-base64 \
      --compartment-id "$COMPARTMENT" \
      --vault-id "$VAULT_ID" \
      --key-id "$KEY_ID" \
      --secret-name "$name" \
      --secret-content-content "$b64" \
      --description "AlgoT platform secret" >/dev/null
    echo "    created  $name"
  fi
}

get_secret() {
  oci secrets secret-bundle get-secret-bundle-by-name \
    --secret-name "$1" --vault-id "$VAULT_ID" \
    --query 'data."secret-bundle-content".content' --raw-output 2>/dev/null \
    | base64 -d
}

case "${1:-}" in
  push)
    echo "==> Writing secrets to vault $VAULT_ID"
    for name in "${ALL_SECRETS[@]}"; do
      IFS=':' read -r file key <<< "$(secret_source "$name")"
      value="$(env_get "$file" "$key" || true)"
      if [[ -z "$value" ]]; then
        echo "    SKIP     $name (no $key in $(basename "$file"))" >&2
        continue
      fi
      put_secret "$name" "$value"
    done
    echo
    echo "Done. Values were read from .env and sent straight to Vault."
    echo "Nothing was written to disk, logged, or committed."
    ;;

  push-token)
    # Read the OCIR auth token without ever displaying it.
    #
    # Interactive: `read -rs` disables terminal echo, so the token is not
    # rendered on screen, and because it is typed at a read prompt rather
    # than a shell prompt it never enters shell history. It is also never an
    # argv value, so it cannot appear in `ps` output.
    #
    # Non-interactive: read from the pipe, so the token can be streamed
    # directly from `oci iam auth-token create` without touching disk.
    if [[ -t 0 ]]; then
      printf 'OCIR auth token (input hidden, press Enter when done): ' >&2
      IFS= read -rs TOKEN
      printf '\n' >&2
    else
      IFS= read -r TOKEN || true
    fi
    # Strip stray whitespace/CR that a copy-paste can carry.
    TOKEN="$(printf '%s' "$TOKEN" | tr -d '\r\n[:space:]')"
    [[ -n "$TOKEN" ]] || { echo "FATAL: empty token" >&2; exit 1; }
    echo "Read a ${#TOKEN}-character token." >&2
    put_secret algot-ocir-auth-token "$TOKEN"
    echo "Stored. Use 'vault-secrets.sh sync' to create the image pull secret."
    ;;

  cosign-key)
    # Generate the container image signing keypair.
    #
    # Local signing cannot use cosign's keyless (Sigstore OIDC) flow: that
    # opens a browser for an interactive identity challenge, which an
    # operator script and a headless session cannot complete. CI keeps
    # keyless signing, where the runner's own OIDC token supplies the
    # identity. So locally-built images are signed with this long-lived key
    # instead, and the public half is committed for verification.
    #
    # The private key and its password go to Vault only. The private key is
    # generated in a mode-700 temporary directory, read straight into Vault
    # and shredded; neither half of the secret is echoed.
    command -v cosign >/dev/null || {
      echo "FATAL: cosign not on PATH. Run:" >&2
      echo '  eval "$(deployment/scripts/install-security-tools.sh --print-path)"' >&2
      exit 1
    }

    PUB_DEST="${COSIGN_PUB:-$REPO_ROOT/deployment/security/cosign.pub}"
    if [[ -f "$PUB_DEST" && "${FORCE_ROTATE:-0}" != "1" ]]; then
      cat >&2 <<MSG
A signing public key already exists at:
  $PUB_DEST

Rotating it invalidates every existing signature, so this is deliberate
rather than automatic. To rotate anyway:

  FORCE_ROTATE=1 $0 cosign-key

MSG
      exit 1
    fi

    WORKDIR="$(mktemp -d)"
    chmod 700 "$WORKDIR"
    # WORKDIR is expanded now, not at trap time, so the trap still refers to
    # this directory if the variable is reassigned later.
    # shellcheck disable=SC2064
    trap "find '$WORKDIR' -type f -exec shred -u {} + 2>/dev/null || true; rm -rf '$WORKDIR'" EXIT

    # 32 random bytes, base64-encoded, as the key password.
    PASSWORD="$(python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())')"

    echo "==> Generating a cosign keypair"
    ( cd "$WORKDIR" && COSIGN_PASSWORD="$PASSWORD" cosign generate-key-pair >/dev/null )

    [[ -s "$WORKDIR/cosign.key" && -s "$WORKDIR/cosign.pub" ]] \
      || { echo "FATAL: cosign did not produce a keypair." >&2; exit 1; }

    put_secret algot-cosign-private-key "$(cat "$WORKDIR/cosign.key")"
    put_secret algot-cosign-password "$PASSWORD"

    mkdir -p "$(dirname "$PUB_DEST")"
    install -m 644 "$WORKDIR/cosign.pub" "$PUB_DEST"

    echo
    echo "Private key and password are in Vault; the private key never"
    echo "remained on disk. Public key written to:"
    echo "  $PUB_DEST"
    echo "Commit the public key -- verification needs it."
    ;;

  sync)
    command -v kubectl >/dev/null || { echo "FATAL: kubectl not found" >&2; exit 1; }
    echo "==> Reading secrets from Vault and applying them to namespace $NS"

    DB_API_KEYS="$(get_secret algot-market-data-db-api-keys)"
    DB_CLIENT_KEY="$(get_secret algot-market-data-db-client-key)"
    ALPACA_KEY="$(get_secret algot-alpaca-api-key)"
    ALPACA_SECRET="$(get_secret algot-alpaca-secret-key)"
    LSE_KEY="$(get_secret algot-lse-api-key || true)"

    for pair in "DB_API_KEYS:$DB_API_KEYS" "DB_CLIENT_KEY:$DB_CLIENT_KEY" \
                "ALPACA_KEY:$ALPACA_KEY" "ALPACA_SECRET:$ALPACA_SECRET"; do
      if [[ -z "${pair#*:}" ]]; then
        echo "FATAL: ${pair%%:*} is missing from Vault. Run '$0 push' first." >&2
        exit 1
      fi
    done

    # The ingestion client key must be one the database accepts, or every
    # write 401s and the failure only shows up at runtime.
    if ! printf '%s' ",$DB_API_KEYS," | grep -qF ",$DB_CLIENT_KEY,"; then
      echo "FATAL: the client key is not among the database's accepted keys." >&2
      exit 1
    fi
    echo "    client key verified against the database's accepted keys"

    # Pass values through stdin to Python and then directly to the API.
    # kubectl --from-literal puts credentials in the process command line;
    # client-side apply also copies Secret data into a last-applied annotation.
    apply_secret() {
      local name="$1"
      shift
      printf '%s\0' "$@" | python3 -c '
import base64, json, sys
parts = sys.stdin.buffer.read().split(b"\0")[:-1]
if len(parts) % 2:
    sys.exit("secret keys and values must be paired")
data = {parts[i].decode(): base64.b64encode(parts[i + 1]).decode()
        for i in range(0, len(parts), 2)}
json.dump({"apiVersion": "v1", "kind": "Secret",
           "metadata": {"name": sys.argv[1], "namespace": "algot-data"},
           "type": "Opaque", "data": data}, sys.stdout)
' "$name" | kubectl apply --server-side --field-manager=algot-vault-sync -f - >/dev/null
      echo "    secret/$name"
    }

    apply_secret market-data-db-auth api-keys "$DB_API_KEYS"
    apply_secret market-data-db-client MARKET_DATA_DB_API_KEY "$DB_CLIENT_KEY"
    apply_secret alpaca-credentials \
      ALPACA_API_KEY "$ALPACA_KEY" \
      ALPACA_SECRET_KEY "$ALPACA_SECRET"

    if [[ -n "$LSE_KEY" ]]; then
      apply_secret lse-credentials LSE_API_KEY "$LSE_KEY"
    else
      echo "    WARNING: no LSE key in Vault; London Strategic Edge backfill will not work." >&2
      apply_secret lse-credentials LSE_API_KEY ""
    fi

    # Image pull secret for the private OCIR repositories.
    TOKEN="$(get_secret algot-ocir-auth-token || true)"
    if [[ -n "$TOKEN" ]]; then
      REGISTRY="$("$TERRAFORM" -chdir="$TF_DIR" output -raw registry_endpoint)"
      OCIR_NS="$("$TERRAFORM" -chdir="$TF_DIR" output -raw registry_namespace)"
      # OCIR's docker username is "<namespace>/<oci-username>". Resolve the
      # username from the API key actually in use -- never from an arbitrary
      # entry in the tenancy's user list. Federated users need the
      # "oracleidentitycloudservice/<email>" form, so allow an override.
      if [[ -z "${OCIR_USERNAME:-}" ]]; then
        USER_OCID="${OCI_CLI_USER:-$(awk -F= '/^user[[:space:]]*=/{gsub(/[[:space:]]/,"",$2); print $2; exit}' "${OCI_CLI_CONFIG_FILE:-$HOME/.oci/config}")}"
        if [[ -z "$USER_OCID" ]]; then
          echo "FATAL: could not determine the OCI user. Set OCIR_USERNAME explicitly." >&2
          exit 1
        fi
        USER_NAME="$(oci iam user get --user-id "$USER_OCID" --query 'data.name' --raw-output)"
      else
        USER_NAME="$OCIR_USERNAME"
      fi
      printf '%s\0' "$REGISTRY" "${OCIR_NS}/${USER_NAME}" "$TOKEN" |
        python3 -c '
import base64, json, sys
server, user, token = sys.stdin.buffer.read().split(b"\0")[:3]
auth = base64.b64encode(user + b":" + token).decode()
config = json.dumps({"auths": {server.decode(): {"username": user.decode(),
    "password": token.decode(), "auth": auth}}}).encode()
json.dump({"apiVersion": "v1", "kind": "Secret",
           "metadata": {"name": "ocir-pull", "namespace": "algot-data"},
           "type": "kubernetes.io/dockerconfigjson",
           "data": {".dockerconfigjson": base64.b64encode(config).decode()}}, sys.stdout)
' | kubectl apply --server-side --field-manager=algot-vault-sync -f - >/dev/null
      echo "    secret/ocir-pull  (registry $REGISTRY, user ${OCIR_NS}/${USER_NAME})"

      for sa in market-data-db data-ingestion algot-maintenance; do
        kubectl patch serviceaccount "$sa" -n "$NS" \
          -p '{"imagePullSecrets":[{"name":"ocir-pull"}]}' >/dev/null 2>&1 || true
      done
      echo "    pull secret attached to service accounts"
    else
      echo "    WARNING: no OCIR auth token in Vault; pods cannot pull private images." >&2
      echo "             Create one in the Console, then: $0 push-token" >&2
    fi

    echo
    echo "Secrets applied from Vault. No value touched disk or stdout."
    ;;

  list)
    # JMESPath backticks are literal query syntax, not command substitution.
    # shellcheck disable=SC2016
    oci vault secret list --compartment-id "$COMPARTMENT" --vault-id "$VAULT_ID" --all \
      --query 'data[?"lifecycle-state"!=`DELETED`].{name:"secret-name",state:"lifecycle-state",version:"current-version-number"}' \
      --output table
    ;;

  *)
    sed -n '2,12p' "$0" >&2
    exit 2
    ;;
esac
