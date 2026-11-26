output "vcn_id" { value = oci_core_vcn.main.id }
output "k8s_api_subnet_id" { value = oci_core_subnet.k8s_api.id }
output "workers_subnet_id" { value = oci_core_subnet.workers.id }
output "nat_gateway_id" { value = oci_core_nat_gateway.main.id }
output "pods_subnet_id" { value = oci_core_subnet.pods.id }
