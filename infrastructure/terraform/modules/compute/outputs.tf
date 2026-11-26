output "node_pool_id" { value = oci_containerengine_node_pool.main.id }
output "total_ocpus" { value = local.total_ocpus }
output "total_memory_gbs" { value = local.total_memory_gbs }
output "total_boot_storage_gbs" { value = var.node_count * var.node_boot_volume_gbs }
output "node_image_id" { value = local.resolved_image_id }
