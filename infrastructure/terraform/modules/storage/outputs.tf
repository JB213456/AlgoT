output "database_volume_id" { value = oci_core_volume.database.id }
output "database_volume_gbs" { value = oci_core_volume.database.size_in_gbs }
output "objectstorage_namespace" { value = data.oci_objectstorage_namespace.main.namespace }
output "backup_bucket_name" { value = oci_objectstorage_bucket.backups.name }
output "total_storage_gbs" { value = local.total_storage_gbs }
