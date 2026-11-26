# Pinned toolchain and providers.
#
# State is deliberately LOCAL, synced to a private, versioned OCI Object
# Storage bucket by scripts/state.sh. The OCI S3-compatible backend would
# require a long-lived Customer Secret Key; keeping state local and syncing
# with the operator's existing API key avoids creating that extra credential.
# See docs/infrastructure/STATE.md.
terraform {
  required_version = "~> 1.14"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 7.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}
