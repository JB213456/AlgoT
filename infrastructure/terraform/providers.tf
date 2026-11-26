# Authentication comes from the operator's ~/.oci/config profile by default.
# CI sets TF_VAR_* / OCI_* environment variables instead and leaves
# config_file_profile null.
provider "oci" {
  region              = var.region
  config_file_profile = var.oci_config_file_profile
  tenancy_ocid        = var.oci_config_file_profile == null ? var.tenancy_ocid : null
  user_ocid           = var.oci_config_file_profile == null ? var.user_ocid : null
  fingerprint         = var.oci_config_file_profile == null ? var.fingerprint : null
  private_key_path    = var.oci_config_file_profile == null ? var.private_key_path : null
}
