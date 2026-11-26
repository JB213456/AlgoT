output "oke_policy_id" { value = oci_identity_policy.oke_service.id }
output "worker_dynamic_group_name" { value = oci_identity_dynamic_group.worker_nodes.name }
output "service_encryption_policy_id" { value = oci_identity_policy.service_encryption.id }
