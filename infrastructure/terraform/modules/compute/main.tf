# Managed Ampere A1 worker node pool.
#
# Total allocation is asserted below against the Always Free ceiling before
# anything is created: an over-allocated plan fails at plan time rather than
# producing billable nodes.

locals {
  total_ocpus      = var.node_count * var.node_ocpus
  total_memory_gbs = var.node_count * var.node_memory_gbs

  # Newest aarch64 image published for this OKE version. Oracle returns
  # sources newest-first, so the first match is the current one.
  aarch64_sources = [
    for s in data.oci_containerengine_node_pool_option.main.sources :
    s if can(regex("aarch64", s.source_name)) &&
    can(regex("OKE-${replace(var.kubernetes_version, "v", "")}-", s.source_name))
  ]

  resolved_image_id = coalesce(
    var.node_image_id,
    try(local.aarch64_sources[0].image_id, null),
  )
}

data "oci_containerengine_node_pool_option" "main" {
  node_pool_option_id = "all"
  compartment_id      = var.compartment_ocid
}

# Fail the plan rather than the bill.
resource "null_resource" "free_tier_assertion" {
  lifecycle {
    precondition {
      condition     = local.total_ocpus <= var.free_tier_max_ocpus
      error_message = "Total OCPUs (${local.total_ocpus}) exceeds the Always Free allowance of ${var.free_tier_max_ocpus}."
    }
    precondition {
      condition     = local.total_memory_gbs <= var.free_tier_max_memory_gbs
      error_message = "Total memory (${local.total_memory_gbs} GB) exceeds the Always Free allowance of ${var.free_tier_max_memory_gbs} GB."
    }
    precondition {
      condition     = local.resolved_image_id != null
      error_message = "No aarch64 OKE image could be resolved for ${var.kubernetes_version}. Set node_image_id explicitly."
    }
  }
}

resource "oci_containerengine_node_pool" "main" {
  cluster_id         = var.cluster_id
  compartment_id     = var.compartment_ocid
  name               = "${var.project_name}-pool-a1"
  kubernetes_version = var.kubernetes_version
  node_shape         = var.node_shape
  freeform_tags      = var.freeform_tags

  node_shape_config {
    ocpus         = var.node_ocpus
    memory_in_gbs = var.node_memory_gbs
  }

  node_source_details {
    source_type             = "IMAGE"
    image_id                = local.resolved_image_id
    boot_volume_size_in_gbs = var.node_boot_volume_gbs
  }

  node_config_details {
    size = var.node_count

    # Encrypt traffic between a node and its boot/block volumes. Free, and
    # supported by every VM.Standard.A1.Flex shape. This changes the pool's
    # launch configuration only: OCI applies it to nodes as they are created,
    # so the two running nodes are not recycled by setting it.
    is_pv_encryption_in_transit_enabled = true

    # Single AD on purpose: block volumes are AD-scoped, so keeping both
    # nodes together lets the database volume re-attach to either one.
    placement_configs {
      availability_domain = var.availability_domain
      subnet_id           = var.workers_subnet_id
    }

    nsg_ids = [var.workers_nsg_id]

    node_pool_pod_network_option_details {
      cni_type       = "OCI_VCN_IP_NATIVE"
      pod_subnet_ids = [var.pods_subnet_id]
      pod_nsg_ids    = [var.pods_nsg_id]
      # A 2-OCPU A1 node gets 2 VNICs, and OKE allows 31 pods per secondary
      # VNIC. 31 is the ceiling this shape supports.
      max_pods_per_node = var.max_pods_per_node
    }

    freeform_tags = var.freeform_tags
  }

  # Drain pods before a node is removed or replaced so the database gets a
  # clean shutdown and its volume detaches properly.
  #
  # 30 minutes is far longer than the database needs (its
  # terminationGracePeriodSeconds is 45s), but force-delete is enabled at the
  # end of it. An earlier PT1H-with-no-force setting deadlocked node
  # replacement: a node that is already NotReady can never finish draining,
  # so without a force step the replacement waits forever and a broken node
  # cannot be recycled at all.
  node_eviction_node_pool_settings {
    # PT30M, not 30M or PT1800S: OCI normalises the value, and the config must
    # match the normalised form or every later plan shows spurious drift.
    eviction_grace_duration              = "PT30M"
    is_force_delete_after_grace_duration = true
  }

  # Cycling stays DISABLED. The surge and unavailable values below are inert
  # while is_node_cycling_enabled is false, but OCI's update API rejects the
  # request unless they are between 1 and 4, so they cannot simply be omitted
  # once the pool exists.
  #
  # They must never be used: enabling cycling with a surge of 1 would add a
  # third node and momentarily take the pool to 6 OCPUs, outside the Always
  # Free allowance. The free-tier gate rejects any plan that enables cycling.
  node_pool_cycling_details {
    is_node_cycling_enabled = false
    maximum_surge           = 1
    maximum_unavailable     = 1
  }

  initial_node_labels {
    key   = "algot.io/pool"
    value = "data"
  }

  depends_on = [null_resource.free_tier_assertion]

  lifecycle {
    # Image changes would recycle both nodes at once; do that deliberately.
    ignore_changes = [node_source_details[0].image_id, kubernetes_version]
  }
}
