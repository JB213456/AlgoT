# OCI Vault for application secrets.
#
# This replaces storing Alpaca, London Strategic Edge and database
# credentials as GitHub repository secrets. The credentials now live in
# Oracle, and the pods reach them with an instance principal, so no
# application secret is held by GitHub at all.
#
# Always Free basis (from Oracle's Always Free resources page):
#   - "150 total Always Free secrets (across any number of vaults)"
#   - "All master encryption keys protected by software are free"
#   - "virtual private vaults are not included in the Always Free resources"
# This deployment holds 8 secrets and two software-protected keys, against an
# allowance of 150 secrets and unlimited software keys.
#
# The tenancy's own limits corroborate it: virtual-vault-count is 10 while
# virtual-private-vault-count is 0, so the billable vault type cannot be
# created here even by mistake.

resource "oci_kms_vault" "main" {
  compartment_id = var.compartment_ocid
  display_name   = "${var.project_name}-vault"

  # DEFAULT is the shared "virtual" vault, which is free. VIRTUAL_PRIVATE
  # bills per hour; the free-tier gate rejects any plan that selects it.
  vault_type = "DEFAULT"

  freeform_tags = var.freeform_tags

  lifecycle {
    # Deleting a vault schedules it for destruction and takes every secret
    # with it, and the name cannot be reused for days afterwards.
    prevent_destroy = true
  }
}

resource "oci_kms_key" "secrets" {
  compartment_id      = var.compartment_ocid
  display_name        = "${var.project_name}-secrets-key"
  management_endpoint = oci_kms_vault.main.management_endpoint

  # SOFTWARE protection is free for any number of key versions. HSM
  # protection is capped at 20 free key versions tenancy-wide, which is a
  # scarce shared resource and unnecessary for this use.
  protection_mode = "SOFTWARE"

  key_shape {
    algorithm = "AES"
    length    = 32
  }

  freeform_tags = var.freeform_tags

  lifecycle {
    # Losing this key makes every secret encrypted under it unrecoverable.
    prevent_destroy = true
  }
}

# Separate key for encryption at rest of the block volume and the Object
# Storage buckets.
#
# Deliberately NOT the secrets key above: worker node instance principals are
# granted "read secret-family", which exercises the secrets key, while this
# key is used only by the blockstorage and objectstorage service principals.
# Splitting them means a change to either grant cannot widen access to the
# other. Software-protected keys are free for any number of versions, so the
# second key costs nothing.
resource "oci_kms_key" "storage" {
  compartment_id      = var.compartment_ocid
  display_name        = "${var.project_name}-storage-key"
  management_endpoint = oci_kms_vault.main.management_endpoint

  protection_mode = "SOFTWARE"

  key_shape {
    algorithm = "AES"
    length    = 32
  }

  freeform_tags = var.freeform_tags

  lifecycle {
    # The Terraform state bucket and the market data volume are encrypted
    # under this key. Losing it makes both unreadable, and state is what
    # makes recovery possible at all.
    prevent_destroy = true
  }
}

# Secret *values* are deliberately NOT managed by Terraform.
#
# An oci_vault_secret resource records its content in Terraform state, which
# would put live Alpaca and database credentials into a state file in plain
# text. Terraform creates the vault and key; the secrets themselves are
# written by deployment/scripts/vault-secrets.sh push, which streams values
# from the local .env files straight to the Vault API.
