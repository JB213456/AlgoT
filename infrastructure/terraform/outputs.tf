output "cluster_id" {
  description = "OKE cluster OCID."
  value       = module.kubernetes.cluster_id
}

output "cluster_name" {
  description = "OKE cluster name."
  value       = module.kubernetes.cluster_name
}

output "cluster_type" {
  description = "Control plane tier. BASIC_CLUSTER is the Always Free tier."
  value       = "BASIC_CLUSTER"
}

output "kubernetes_version" {
  description = "Cluster Kubernetes version."
  value       = module.kubernetes.kubernetes_version
}

output "kubernetes_public_endpoint" {
  description = "Public Kubernetes API endpoint."
  value       = module.kubernetes.public_endpoint
}

output "node_pool_id" {
  description = "Worker node pool OCID."
  value       = module.compute.node_pool_id
}

output "vcn_id" {
  description = "VCN OCID."
  value       = module.networking.vcn_id
}

output "workers_subnet_id" {
  description = "Private worker subnet OCID."
  value       = module.networking.workers_subnet_id
}

output "database_volume_id" {
  description = "OCID of the protected market data block volume. Referenced by the static PersistentVolume."
  value       = module.storage.database_volume_id
}

output "objectstorage_namespace" {
  description = "Object Storage namespace for backups and state sync."
  value       = module.storage.objectstorage_namespace
}

output "backup_bucket_name" {
  description = "Bucket holding database and checkpoint backups."
  value       = module.storage.backup_bucket_name
}

output "availability_domain" {
  description = "AD hosting both worker nodes and the database volume."
  value       = var.availability_domain
}

output "free_tier_allocation" {
  description = "Provisioned allocation against the Always Free ceilings."
  value = {
    ocpus_used        = module.compute.total_ocpus
    ocpus_limit       = var.free_tier_max_ocpus
    memory_gbs_used   = module.compute.total_memory_gbs
    memory_gbs_limit  = var.free_tier_max_memory_gbs
    boot_storage_gbs  = module.compute.total_boot_storage_gbs
    block_storage_gbs = module.storage.database_volume_gbs
    storage_gbs_used  = module.storage.total_storage_gbs
    storage_gbs_limit = var.free_tier_max_storage_gbs
    storage_reserve   = var.free_tier_max_storage_gbs - module.storage.total_storage_gbs
  }
}

output "region" {
  description = "OCI region hosting the deployment."
  value       = var.region
}

output "compartment_ocid" {
  description = "Compartment holding every AlgoT resource."
  value       = var.compartment_ocid
}

output "k8s_api_nsg_id" {
  description = "NSG guarding the Kubernetes API endpoint. CI grants and revokes its own address here."
  value       = module.security.k8s_api_nsg_id
}

output "registry_endpoint" {
  description = "OCIR endpoint. Images are pushed and pulled here, never from a public registry."
  value       = module.registry.registry_endpoint
}

output "registry_namespace" {
  description = "OCIR namespace (the tenancy's object storage namespace)."
  value       = module.registry.registry_namespace
}

output "repository_urls" {
  description = "Private image paths for each service."
  value       = module.registry.repository_urls
}

output "vault_id" {
  description = "OCI Vault holding the application secrets."
  value       = module.vault.vault_id
}

output "vault_key_id" {
  description = "Software-protected master key encrypting the secrets."
  value       = module.vault.key_id
}

output "tenancy_ocid" {
  description = "Tenancy OCID, used by the cost verification script."
  value       = var.tenancy_ocid
}
