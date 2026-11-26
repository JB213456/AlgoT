# Defence-in-depth grant for the cluster and node pool resource principals used
# by VCN-native pod networking, scoped to those two exact principals and to the
# single compartment that holds this cluster's workers and network resources.
#
# Deliberately read/use only. "manage instances" was removed on 2026-11-25 after
# the 2026-11-24 PodNetworkTimeOut incident: it was added on the assumption that
# pod VNIC attachment was being denied, but the OCI Audit log for the whole
# failure window (14:00-17:00Z) contains zero 401/403 responses on any
# VNIC, private-IP, subnet or NSG operation, and AttachVnic returned 200 at
# 14:25:53 and 14:50:33 -- both before this policy first existed at 15:16:31.
# Pod VNIC attachment therefore never depended on it. Oracle documents
# "manage instances" for VCN-native pod networking only when the cluster and its
# VCN/node pool live in DIFFERENT compartments; every resource here is in the
# same compartment, so no pod-networking policy is strictly required at all.
# See docs: Content/ContEng/Concepts/contengpodnetworking_topic-OCI_CNI_plugin.htm
#
# The remaining "use" verbs grant no create, delete or terminate rights. They are
# kept as a safety net only because the claim above cannot be re-proven without
# recycling a node, which risks losing Always Free Ampere A1 capacity. They can
# be dropped entirely at the next planned node recycle.
resource "oci_identity_policy" "cluster_principal" {
  compartment_id = var.tenancy_ocid
  name           = "${var.project_name}-cluster-principal-policy"
  description    = "Least-privilege VCN-native pod networking permissions for AlgoT OKE"
  freeform_tags  = var.freeform_tags

  statements = [
    for permission in [
      "use private-ips",
      "use network-security-groups",
      "use subnets",
      "use vnics",
      "use vnic-attachments",
    ] : "Allow any-user to ${permission} in compartment id ${var.compartment_ocid} where any { request.principal.id = '${var.cluster_ocid}', request.principal.id = '${var.node_pool_ocid}' }"
  ]
}
