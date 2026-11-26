output "k8s_api_nsg_id" { value = oci_core_network_security_group.k8s_api.id }
output "workers_nsg_id" { value = oci_core_network_security_group.workers.id }
output "pods_nsg_id" { value = oci_core_network_security_group.pods.id }
