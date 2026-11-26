# Persistent storage for the data services.
#
# The database volume is created here rather than dynamically by the CSI
# driver so that Terraform owns its lifecycle and can refuse to destroy it.
# kubernetes/database/ binds it through a static PersistentVolume with a
# Retain reclaim policy, so deleting the PVC -- or the whole namespace --
# cannot delete the market data.

data "oci_objectstorage_namespace" "main" {
  compartment_id = var.compartment_ocid
}

locals {
  total_storage_gbs = var.node_boot_storage_gbs + var.database_volume_gbs
}

resource "null_resource" "free_tier_storage_assertion" {
  # Carries the IAM grant that lets the blockstorage service use the
  # encryption key, so the volume below -- which depends on this resource --
  # is never created or re-keyed before the grant exists.
  #
  # A `depends_on = [module.identity]` on this whole module would do the same
  # job, but it also defers data.oci_objectstorage_namespace to apply time,
  # which makes `namespace` unknown and forces BOTH buckets to be replaced.
  # Carrying the dependency as data keeps the data source readable at plan
  # time.
  triggers = {
    encryption_policy_id = var.encryption_policy_id
  }

  lifecycle {
    precondition {
      condition     = local.total_storage_gbs <= var.free_tier_max_storage_gbs
      error_message = "Total storage (${local.total_storage_gbs} GB = ${var.node_boot_storage_gbs} GB boot + ${var.database_volume_gbs} GB database) exceeds the Always Free allowance of ${var.free_tier_max_storage_gbs} GB."
    }
  }
}

resource "oci_core_volume" "database" {
  compartment_id      = var.compartment_ocid
  availability_domain = var.availability_domain
  display_name        = "${var.project_name}-market-data-db"
  size_in_gbs         = var.database_volume_gbs

  # Balanced (10 VPU) is the Always Free default and what OKE boot volumes
  # use. Raising this bills Block Volume Performance Units.
  vpus_per_gb = 10

  # Customer-managed key rather than Oracle's default key, so the operator
  # controls the key the market data is encrypted under. Key Management is
  # free for software-protected keys, so this adds no cost. Changing it is an
  # in-place re-wrap of the volume's data encryption key -- the data is not
  # rewritten and the volume is not replaced.
  kms_key_id = var.kms_key_id

  # OCI rejects "/" in freeform tag keys, so these are flat names. The
  # free-tier gate matches on algot-role to refuse any plan that would
  # destroy or replace this volume.
  freeform_tags = merge(var.freeform_tags, {
    "algot-role"      = "market-data-database"
    "algot-protected" = "true"
  })

  # The blockstorage service principal must already be allowed to use the key,
  # or attaching it fails as if the key did not exist.
  depends_on = [null_resource.free_tier_storage_assertion]

  lifecycle {
    # The market data lives here. Nothing in Terraform may replace or remove
    # it; a size change or AD move would destroy and recreate the volume.
    prevent_destroy = true

    ignore_changes = [size_in_gbs]
  }
}

# Backups for the database and the ingestion checkpoint store. Object Storage
# gives 20 GB free, which is separate from the 200 GB block volume allowance.
resource "oci_objectstorage_bucket" "backups" {
  compartment_id = var.compartment_ocid
  namespace      = data.oci_objectstorage_namespace.main.namespace
  name           = var.backup_bucket_name
  access_type    = "NoPublicAccess"
  storage_tier   = "Standard"
  versioning     = "Enabled"
  kms_key_id     = var.kms_key_id
  freeform_tags  = var.freeform_tags

  lifecycle {
    prevent_destroy = true
  }
}

# Bounds backup growth so Object Storage stays inside the 20 GB free
# allowance. OCI refuses a retention rule on a versioned bucket, so expiry
# is expressed as a lifecycle policy instead.
resource "oci_objectstorage_object_lifecycle_policy" "backups" {
  namespace = data.oci_objectstorage_namespace.main.namespace
  bucket    = oci_objectstorage_bucket.backups.name

  rules {
    name        = "expire-database-backups"
    action      = "DELETE"
    time_amount = 30
    time_unit   = "DAYS"
    is_enabled  = true

    object_name_filter {
      inclusion_prefixes = ["database/"]
    }
  }

  # Checkpoint snapshots are written every few minutes; they are only useful
  # until the next one lands, so they expire quickly.
  rules {
    name        = "expire-checkpoint-snapshots"
    action      = "DELETE"
    time_amount = 7
    time_unit   = "DAYS"
    is_enabled  = true

    object_name_filter {
      inclusion_prefixes = ["checkpoints/"]
    }
  }

  # Non-current versions are retained briefly for recovery, then removed.
  rules {
    name        = "expire-previous-versions"
    action      = "DELETE"
    target      = "previous-object-versions"
    time_amount = 14
    time_unit   = "DAYS"
    is_enabled  = true
  }
}

# Created during bootstrap before Terraform had state to store; imported so
# that Terraform manages every resource in the project.
resource "oci_objectstorage_bucket" "terraform_state" {
  compartment_id = var.compartment_ocid
  namespace      = data.oci_objectstorage_namespace.main.namespace
  name           = var.state_bucket_name
  access_type    = "NoPublicAccess"
  storage_tier   = "Standard"
  versioning     = "Enabled"
  kms_key_id     = var.kms_key_id
  freeform_tags  = var.freeform_tags

  lifecycle {
    prevent_destroy = true
  }
}
