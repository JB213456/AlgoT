# VCN plus the gateways and routing OKE needs.
#
# Every gateway used here (internet, NAT, service) carries no hourly charge;
# outbound data transfer is free to 10 TB/month, far beyond this workload.
# No load balancer is created -- all services are ClusterIP only.

resource "oci_core_vcn" "main" {
  compartment_id = var.compartment_ocid
  cidr_blocks    = [var.vcn_cidr]
  display_name   = "${var.project_name}-vcn"
  dns_label      = replace(var.project_name, "-", "")
  freeform_tags  = var.freeform_tags
}

resource "oci_core_internet_gateway" "main" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.main.id
  display_name   = "${var.project_name}-igw"
  enabled        = true
  freeform_tags  = var.freeform_tags
}

# Lets private worker nodes reach GHCR, Alpaca and London Strategic Edge
# without giving them public IPs.
resource "oci_core_nat_gateway" "main" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.main.id
  display_name   = "${var.project_name}-nat"
  block_traffic  = false
  freeform_tags  = var.freeform_tags
}

data "oci_core_services" "all" {}

locals {
  oracle_services_network = one([
    for service in data.oci_core_services.all.services : service
    if startswith(service.name, "All ")
  ])
}

# Keeps OCI service traffic (Object Storage backups, image pulls from OCIR)
# on Oracle's network instead of out through the NAT gateway.
resource "oci_core_service_gateway" "main" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.main.id
  display_name   = "${var.project_name}-sgw"
  freeform_tags  = var.freeform_tags

  services {
    service_id = local.oracle_services_network.id
  }
}

resource "oci_core_route_table" "public" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.main.id
  display_name   = "${var.project_name}-rt-public"
  freeform_tags  = var.freeform_tags

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.main.id
  }
}

resource "oci_core_route_table" "private" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.main.id
  display_name   = "${var.project_name}-rt-private"
  freeform_tags  = var.freeform_tags

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_nat_gateway.main.id
  }

  route_rules {
    destination       = local.oracle_services_network.cidr_block
    destination_type  = "SERVICE_CIDR_BLOCK"
    network_entity_id = oci_core_service_gateway.main.id
  }
}

# Security lists are left empty on purpose: all filtering is done by the
# network security groups in the security module, so there is exactly one
# place to reason about ingress and egress.
resource "oci_core_security_list" "empty" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.main.id
  display_name   = "${var.project_name}-sl-empty"
  freeform_tags  = var.freeform_tags
}

resource "oci_core_subnet" "k8s_api" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.main.id
  cidr_block                 = var.k8s_api_subnet_cidr
  display_name               = "${var.project_name}-subnet-k8s-api"
  dns_label                  = "k8sapi"
  prohibit_public_ip_on_vnic = false
  route_table_id             = oci_core_route_table.public.id
  security_list_ids          = [oci_core_security_list.empty.id]
  freeform_tags              = var.freeform_tags
}

resource "oci_core_subnet" "workers" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.main.id
  cidr_block                 = var.workers_subnet_cidr
  display_name               = "${var.project_name}-subnet-workers"
  dns_label                  = "workers"
  prohibit_public_ip_on_vnic = true
  prohibit_internet_ingress  = true
  route_table_id             = oci_core_route_table.private.id
  security_list_ids          = [oci_core_security_list.empty.id]
  freeform_tags              = var.freeform_tags
}

# Pod subnet for the VCN-Native Pod Networking CNI.
#
# Flannel was the original choice, but Oracle does not support running the
# Calico policy engine alongside flannel ("causes network issues"), and
# flannel alone silently ignores NetworkPolicy objects. VCN-native pod
# networking is the only OKE configuration where Calico policy enforcement
# is supported, so pods draw real VCN IPs from this subnet.
resource "oci_core_subnet" "pods" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.main.id
  cidr_block                 = var.pods_subnet_cidr
  display_name               = "${var.project_name}-subnet-pods"
  dns_label                  = "pods"
  prohibit_public_ip_on_vnic = true
  prohibit_internet_ingress  = true
  route_table_id             = oci_core_route_table.private.id
  security_list_ids          = [oci_core_security_list.empty.id]
  freeform_tags              = var.freeform_tags
}
