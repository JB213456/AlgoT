# Network security groups implementing OKE's required flows and nothing more.
#
# Outbound from the workers is restricted to 443/80 plus the Oracle Services
# Network, which is all GHCR, Alpaca and London Strategic Edge need. There is
# no blanket 0.0.0.0/0 all-protocol egress rule.

data "oci_core_services" "all" {}

locals {
  oracle_services_network = one([
    for service in data.oci_core_services.all.services : service
    if startswith(service.name, "All ")
  ])
}

# ---------------------------------------------------------------------------
# Kubernetes API endpoint
# ---------------------------------------------------------------------------
resource "oci_core_network_security_group" "k8s_api" {
  compartment_id = var.compartment_ocid
  vcn_id         = var.vcn_id
  display_name   = "${var.project_name}-nsg-k8s-api"
  freeform_tags  = var.freeform_tags
}

# Operator kubectl access. No rules exist at all when api_allowed_cidrs is
# empty, so the endpoint is unreachable from the internet by default.
resource "oci_core_network_security_group_security_rule" "api_ingress_operator" {
  for_each = toset(var.api_allowed_cidrs)

  network_security_group_id = oci_core_network_security_group.k8s_api.id
  direction                 = "INGRESS"
  protocol                  = "6" # TCP
  source                    = each.value
  source_type               = "CIDR_BLOCK"
  description               = "Operator kubectl to the Kubernetes API"

  tcp_options {
    destination_port_range {
      min = 6443
      max = 6443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "api_ingress_workers_6443" {
  network_security_group_id = oci_core_network_security_group.k8s_api.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = var.workers_subnet_cidr
  source_type               = "CIDR_BLOCK"
  description               = "Worker nodes to the Kubernetes API"

  tcp_options {
    destination_port_range {
      min = 6443
      max = 6443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "api_ingress_workers_12250" {
  network_security_group_id = oci_core_network_security_group.k8s_api.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = var.workers_subnet_cidr
  source_type               = "CIDR_BLOCK"
  description               = "Worker nodes to OKE control plane (12250)"

  tcp_options {
    destination_port_range {
      min = 12250
      max = 12250
    }
  }
}

resource "oci_core_network_security_group_security_rule" "api_ingress_workers_icmp" {
  network_security_group_id = oci_core_network_security_group.k8s_api.id
  direction                 = "INGRESS"
  protocol                  = "1" # ICMP
  source                    = var.workers_subnet_cidr
  source_type               = "CIDR_BLOCK"
  description               = "Path MTU discovery from worker nodes"

  icmp_options {
    type = 3
    code = 4
  }
}

resource "oci_core_network_security_group_security_rule" "api_egress_workers" {
  network_security_group_id = oci_core_network_security_group.k8s_api.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination               = var.workers_subnet_cidr
  destination_type          = "CIDR_BLOCK"
  description               = "Control plane to kubelet and node ports"
}

resource "oci_core_network_security_group_security_rule" "api_egress_workers_icmp" {
  network_security_group_id = oci_core_network_security_group.k8s_api.id
  direction                 = "EGRESS"
  protocol                  = "1"
  destination               = var.workers_subnet_cidr
  destination_type          = "CIDR_BLOCK"
  description               = "Path MTU discovery to worker nodes"

  icmp_options {
    type = 3
    code = 4
  }
}

resource "oci_core_network_security_group_security_rule" "api_egress_services" {
  network_security_group_id = oci_core_network_security_group.k8s_api.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination               = local.oracle_services_network.cidr_block
  destination_type          = "SERVICE_CIDR_BLOCK"
  description               = "Control plane to the Oracle Services Network"

  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
}

# ---------------------------------------------------------------------------
# Worker nodes
# ---------------------------------------------------------------------------
resource "oci_core_network_security_group" "workers" {
  compartment_id = var.compartment_ocid
  vcn_id         = var.vcn_id
  display_name   = "${var.project_name}-nsg-workers"
  freeform_tags  = var.freeform_tags
}

# Node-to-node, both directions, covers the flannel VXLAN overlay (8472/udp)
# and Calico's policy distribution.
resource "oci_core_network_security_group_security_rule" "workers_ingress_self" {
  network_security_group_id = oci_core_network_security_group.workers.id
  direction                 = "INGRESS"
  protocol                  = "all"
  source                    = oci_core_network_security_group.workers.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description               = "Node-to-node and pod overlay traffic"
}

resource "oci_core_network_security_group_security_rule" "workers_egress_self" {
  network_security_group_id = oci_core_network_security_group.workers.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = oci_core_network_security_group.workers.id
  destination_type          = "NETWORK_SECURITY_GROUP"
  description               = "Node-to-node and pod overlay traffic"
}

resource "oci_core_network_security_group_security_rule" "workers_ingress_api" {
  network_security_group_id = oci_core_network_security_group.workers.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = var.k8s_api_subnet_cidr
  source_type               = "CIDR_BLOCK"
  description               = "Control plane to kubelet"
}

resource "oci_core_network_security_group_security_rule" "workers_ingress_icmp" {
  network_security_group_id = oci_core_network_security_group.workers.id
  direction                 = "INGRESS"
  protocol                  = "1"
  source                    = var.vcn_cidr_for_icmp
  source_type               = "CIDR_BLOCK"
  description               = "Path MTU discovery inside the VCN"

  icmp_options {
    type = 3
    code = 4
  }
}

resource "oci_core_network_security_group_security_rule" "workers_egress_api_6443" {
  network_security_group_id = oci_core_network_security_group.workers.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination               = var.k8s_api_subnet_cidr
  destination_type          = "CIDR_BLOCK"
  description               = "Kubelet to the Kubernetes API"

  tcp_options {
    destination_port_range {
      min = 6443
      max = 6443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "workers_egress_api_12250" {
  network_security_group_id = oci_core_network_security_group.workers.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination               = var.k8s_api_subnet_cidr
  destination_type          = "CIDR_BLOCK"
  description               = "Kubelet to OKE control plane (12250)"

  tcp_options {
    destination_port_range {
      min = 12250
      max = 12250
    }
  }
}

resource "oci_core_network_security_group_security_rule" "workers_egress_services" {
  network_security_group_id = oci_core_network_security_group.workers.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination               = local.oracle_services_network.cidr_block
  destination_type          = "SERVICE_CIDR_BLOCK"
  description               = "Object Storage backups and OCI service APIs"

  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
}

# Egress to the internet is limited to HTTPS and HTTP: GHCR image pulls,
# the Alpaca REST/websocket APIs and London Strategic Edge exports.
resource "oci_core_network_security_group_security_rule" "workers_egress_https" {
  network_security_group_id = oci_core_network_security_group.workers.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination               = "0.0.0.0/0"
  destination_type          = "CIDR_BLOCK"
  description               = "HTTPS to GHCR, Alpaca and London Strategic Edge"

  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "workers_egress_http" {
  network_security_group_id = oci_core_network_security_group.workers.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination               = "0.0.0.0/0"
  destination_type          = "CIDR_BLOCK"
  description               = "HTTP for OS package metadata and CRL/OCSP checks"

  tcp_options {
    destination_port_range {
      min = 80
      max = 80
    }
  }
}

# ---------------------------------------------------------------------------
# Pods (VCN-native pod networking)
# ---------------------------------------------------------------------------
# With VCN-native networking each pod holds a real VCN IP, so pod traffic is
# subject to NSG rules as well as Kubernetes NetworkPolicy. This NSG is the
# coarse outer boundary; the fine-grained per-workload rules are the
# NetworkPolicy objects that Calico enforces.
resource "oci_core_network_security_group" "pods" {
  compartment_id = var.compartment_ocid
  vcn_id         = var.vcn_id
  display_name   = "${var.project_name}-nsg-pods"
  freeform_tags  = var.freeform_tags
}

# Pod-to-pod and node-to-pod within the cluster.
resource "oci_core_network_security_group_security_rule" "pods_ingress_self" {
  network_security_group_id = oci_core_network_security_group.pods.id
  direction                 = "INGRESS"
  protocol                  = "all"
  source                    = oci_core_network_security_group.pods.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description               = "Pod-to-pod traffic"
}

resource "oci_core_network_security_group_security_rule" "pods_egress_self" {
  network_security_group_id = oci_core_network_security_group.pods.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = oci_core_network_security_group.pods.id
  destination_type          = "NETWORK_SECURITY_GROUP"
  description               = "Pod-to-pod traffic"
}

resource "oci_core_network_security_group_security_rule" "pods_ingress_workers" {
  network_security_group_id = oci_core_network_security_group.pods.id
  direction                 = "INGRESS"
  protocol                  = "all"
  source                    = oci_core_network_security_group.workers.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description               = "Kubelet and node-local traffic to pods"
}

resource "oci_core_network_security_group_security_rule" "pods_egress_workers" {
  network_security_group_id = oci_core_network_security_group.pods.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = oci_core_network_security_group.workers.id
  destination_type          = "NETWORK_SECURITY_GROUP"
  description               = "Pod traffic to node services, including DNS"
}

# Pods reach the Kubernetes API (the CSI driver and Calico's own components).
resource "oci_core_network_security_group_security_rule" "pods_egress_api" {
  network_security_group_id = oci_core_network_security_group.pods.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination               = var.k8s_api_subnet_cidr
  destination_type          = "CIDR_BLOCK"
  description               = "Pods to the Kubernetes API"

  tcp_options {
    destination_port_range {
      min = 6443
      max = 6443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "pods_egress_services" {
  network_security_group_id = oci_core_network_security_group.pods.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination               = local.oracle_services_network.cidr_block
  destination_type          = "SERVICE_CIDR_BLOCK"
  description               = "Object Storage backups over the service gateway"

  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
}

# Outbound to GHCR, Alpaca and London Strategic Edge. HTTPS only.
resource "oci_core_network_security_group_security_rule" "pods_egress_https" {
  network_security_group_id = oci_core_network_security_group.pods.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination               = "0.0.0.0/0"
  destination_type          = "CIDR_BLOCK"
  description               = "HTTPS to GHCR, Alpaca and London Strategic Edge"

  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
}

# The Kubernetes API must reach webhook and pod ports.
resource "oci_core_network_security_group_security_rule" "pods_ingress_api" {
  network_security_group_id = oci_core_network_security_group.pods.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = var.k8s_api_subnet_cidr
  source_type               = "CIDR_BLOCK"
  description               = "Control plane to pod webhook and health ports"
}

resource "oci_core_network_security_group_security_rule" "workers_ingress_pods" {
  network_security_group_id = oci_core_network_security_group.workers.id
  direction                 = "INGRESS"
  protocol                  = "all"
  source                    = oci_core_network_security_group.pods.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description               = "Pod traffic to node services, including DNS"
}

resource "oci_core_network_security_group_security_rule" "workers_egress_pods" {
  network_security_group_id = oci_core_network_security_group.workers.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = oci_core_network_security_group.pods.id
  destination_type          = "NETWORK_SECURITY_GROUP"
  description               = "Kubelet to pods"
}

# ---------------------------------------------------------------------------
# VCN-native pod networking: required flows involving the pod CIDR
# ---------------------------------------------------------------------------
# Oracle's network configuration reference requires the Kubernetes API
# endpoint to accept traffic from the POD CIDR on 6443 and 12250, and to be
# able to reach pods. Without these the CNI cannot register a node's
# NativePodNetwork resource and the node stays NotReady.

resource "oci_core_network_security_group_security_rule" "api_ingress_pods_6443" {
  network_security_group_id = oci_core_network_security_group.k8s_api.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = var.pods_subnet_cidr
  source_type               = "CIDR_BLOCK"
  description               = "Pods to the Kubernetes API"

  tcp_options {
    destination_port_range {
      min = 6443
      max = 6443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "api_ingress_pods_12250" {
  network_security_group_id = oci_core_network_security_group.k8s_api.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = var.pods_subnet_cidr
  source_type               = "CIDR_BLOCK"
  description               = "Pods to OKE control plane (12250)"

  tcp_options {
    destination_port_range {
      min = 12250
      max = 12250
    }
  }
}

resource "oci_core_network_security_group_security_rule" "api_egress_pods" {
  network_security_group_id = oci_core_network_security_group.k8s_api.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = var.pods_subnet_cidr
  destination_type          = "CIDR_BLOCK"
  description               = "Control plane to pods"
}

# CIDR-based worker<->pod rules in addition to the NSG-to-NSG rules above.
# The NSG rules only apply once a pod VNIC has been created and placed in the
# pod NSG; these cover the bootstrap window before that happens.
resource "oci_core_network_security_group_security_rule" "workers_ingress_pods_cidr" {
  network_security_group_id = oci_core_network_security_group.workers.id
  direction                 = "INGRESS"
  protocol                  = "all"
  source                    = var.pods_subnet_cidr
  source_type               = "CIDR_BLOCK"
  description               = "Pod traffic to worker nodes"
}

resource "oci_core_network_security_group_security_rule" "workers_egress_pods_cidr" {
  network_security_group_id = oci_core_network_security_group.workers.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = var.pods_subnet_cidr
  destination_type          = "CIDR_BLOCK"
  description               = "Worker nodes to pods"
}

resource "oci_core_network_security_group_security_rule" "pods_ingress_api_cidr" {
  network_security_group_id = oci_core_network_security_group.pods.id
  direction                 = "INGRESS"
  protocol                  = "all"
  source                    = var.k8s_api_subnet_cidr
  source_type               = "CIDR_BLOCK"
  description               = "Control plane to pods"
}

resource "oci_core_network_security_group_security_rule" "pods_ingress_workers_cidr" {
  network_security_group_id = oci_core_network_security_group.pods.id
  direction                 = "INGRESS"
  protocol                  = "all"
  source                    = var.workers_subnet_cidr
  source_type               = "CIDR_BLOCK"
  description               = "Worker nodes to pods"
}

resource "oci_core_network_security_group_security_rule" "pods_egress_pods_cidr" {
  network_security_group_id = oci_core_network_security_group.pods.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = var.pods_subnet_cidr
  destination_type          = "CIDR_BLOCK"
  description               = "Pod-to-pod traffic across nodes"
}

resource "oci_core_network_security_group_security_rule" "pods_ingress_pods_cidr" {
  network_security_group_id = oci_core_network_security_group.pods.id
  direction                 = "INGRESS"
  protocol                  = "all"
  source                    = var.pods_subnet_cidr
  source_type               = "CIDR_BLOCK"
  description               = "Pod-to-pod traffic across nodes"
}
