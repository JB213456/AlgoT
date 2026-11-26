# OKE BASIC cluster.
#
# BASIC_CLUSTER is the only Always Free control plane: enhanced clusters bill
# per cluster per hour. The tenancy's container-engine cluster-count limit is
# 1, so this must remain the single cluster.
#
# No service_lb_subnet_ids are configured, which means a LoadBalancer Service
# cannot be created by accident -- load balancers are a billed resource.

resource "oci_containerengine_cluster" "main" {
  compartment_id     = var.compartment_ocid
  name               = "${var.project_name}-oke"
  kubernetes_version = var.kubernetes_version
  vcn_id             = var.vcn_id
  type               = "BASIC_CLUSTER"
  freeform_tags      = var.freeform_tags

  endpoint_config {
    subnet_id            = var.k8s_api_subnet_id
    nsg_ids              = [var.k8s_api_nsg_id]
    is_public_ip_enabled = true
  }

  # OCI_VCN_IP_NATIVE, not FLANNEL_OVERLAY: Oracle does not support running
  # the Calico policy engine alongside flannel, and flannel on its own
  # silently ignores NetworkPolicy. VCN-native pod networking is the only
  # OKE configuration in which NetworkPolicy is actually enforced.
  cluster_pod_network_options {
    cni_type = "OCI_VCN_IP_NATIVE"
  }

  options {
    # pods_cidr is not set: with VCN-native networking pod addresses come
    # from the pod subnet, not from an overlay CIDR.
    kubernetes_network_config {
      services_cidr = var.services_cidr
    }

    # Both are deprecated attack surface and neither is needed here.
    add_ons {
      is_kubernetes_dashboard_enabled = false
      is_tiller_enabled               = false
    }

    admission_controller_options {
      is_pod_security_policy_enabled = false
    }
  }

  lifecycle {
    # Deliberately NOT prevent_destroy. The tenancy allows exactly one
    # cluster, so any change that forces replacement must destroy this one
    # first -- a lifecycle guard here only produces an un-appliable plan.
    # The guard that matters lives in policies/free-tier/check_plan.py, which
    # rejects any plan destroying the cluster unless the operator passes
    # --allow-cluster-replacement, and in the retained database volume, which
    # is a separate resource that cluster replacement never touches.
    ignore_changes = [kubernetes_version]
  }
}
