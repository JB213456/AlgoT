# AlgoT shared infrastructure.
#
# Provisions the full platform foundation -- network, OKE Basic cluster, both
# Ampere A1 worker nodes, IAM and persistent storage -- sized so that the
# future algot-strategies, algot-core and algot-monitoring namespaces drop
# onto the same cluster without rebuilding it.
#
# Every resource here is Always Free eligible. See policies/free-tier for the
# automated plan check that enforces it.

data "oci_identity_compartment" "main" {
  id = var.compartment_ocid
}

module "networking" {
  source = "./modules/networking"

  compartment_ocid    = var.compartment_ocid
  project_name        = var.project_name
  freeform_tags       = var.freeform_tags
  vcn_cidr            = var.vcn_cidr
  k8s_api_subnet_cidr = var.k8s_api_subnet_cidr
  workers_subnet_cidr = var.workers_subnet_cidr
  pods_subnet_cidr    = var.pods_subnet_cidr
}

module "security" {
  source = "./modules/security"

  compartment_ocid    = var.compartment_ocid
  project_name        = var.project_name
  freeform_tags       = var.freeform_tags
  vcn_id              = module.networking.vcn_id
  k8s_api_subnet_cidr = var.k8s_api_subnet_cidr
  workers_subnet_cidr = var.workers_subnet_cidr
  vcn_cidr_for_icmp   = var.vcn_cidr
  pods_subnet_cidr    = var.pods_subnet_cidr
  api_allowed_cidrs   = var.api_allowed_cidrs
}

module "identity_cluster" {
  source = "./modules/identity_cluster"

  tenancy_ocid     = var.tenancy_ocid
  compartment_ocid = var.compartment_ocid
  project_name     = var.project_name
  freeform_tags    = var.freeform_tags
  cluster_ocid     = module.kubernetes.cluster_id
  node_pool_ocid   = module.compute.node_pool_id
}

module "identity" {
  source = "./modules/identity"

  tenancy_ocid     = var.tenancy_ocid
  compartment_ocid = var.compartment_ocid
  compartment_name = data.oci_identity_compartment.main.name
  project_name     = var.project_name
  freeform_tags    = var.freeform_tags
  region           = var.region
}

module "kubernetes" {
  source = "./modules/kubernetes"

  compartment_ocid   = var.compartment_ocid
  project_name       = var.project_name
  freeform_tags      = var.freeform_tags
  vcn_id             = module.networking.vcn_id
  k8s_api_subnet_id  = module.networking.k8s_api_subnet_id
  k8s_api_nsg_id     = module.security.k8s_api_nsg_id
  kubernetes_version = var.kubernetes_version
  services_cidr      = var.services_cidr

  depends_on = [module.identity]
}

module "compute" {
  source = "./modules/compute"

  compartment_ocid         = var.compartment_ocid
  project_name             = var.project_name
  freeform_tags            = var.freeform_tags
  cluster_id               = module.kubernetes.cluster_id
  kubernetes_version       = var.kubernetes_version
  availability_domain      = var.availability_domain
  workers_subnet_id        = module.networking.workers_subnet_id
  workers_nsg_id           = module.security.workers_nsg_id
  pods_subnet_id           = module.networking.pods_subnet_id
  pods_nsg_id              = module.security.pods_nsg_id
  node_shape               = var.node_shape
  node_count               = var.node_count
  node_ocpus               = var.node_ocpus
  node_memory_gbs          = var.node_memory_gbs
  node_boot_volume_gbs     = var.node_boot_volume_gbs
  node_image_id            = var.node_image_id
  free_tier_max_ocpus      = var.free_tier_max_ocpus
  free_tier_max_memory_gbs = var.free_tier_max_memory_gbs
}

module "storage" {
  source = "./modules/storage"

  compartment_ocid          = var.compartment_ocid
  project_name              = var.project_name
  freeform_tags             = var.freeform_tags
  availability_domain       = var.availability_domain
  database_volume_gbs       = var.database_volume_gbs
  state_bucket_name         = var.state_bucket_name
  backup_bucket_name        = var.backup_bucket_name
  node_boot_storage_gbs     = var.node_count * var.node_boot_volume_gbs
  free_tier_max_storage_gbs = var.free_tier_max_storage_gbs
  kms_key_id                = module.vault.storage_key_id

  # Passed as a value, not depends_on: a module-level depends_on would defer
  # this module's Object Storage namespace data source to apply time, and an
  # unknown namespace forces both buckets to be replaced.
  encryption_policy_id = module.identity.service_encryption_policy_id
}

module "registry" {
  source = "./modules/registry"

  compartment_ocid = var.compartment_ocid
  project_name     = var.project_name
  freeform_tags    = var.freeform_tags
  repositories     = var.container_repositories
}

module "vault" {
  source = "./modules/vault"

  compartment_ocid = var.compartment_ocid
  project_name     = var.project_name
  freeform_tags    = var.freeform_tags
}
