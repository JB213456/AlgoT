output "cluster_id" { value = oci_containerengine_cluster.main.id }
output "cluster_name" { value = oci_containerengine_cluster.main.name }
output "kubernetes_version" { value = oci_containerengine_cluster.main.kubernetes_version }
output "public_endpoint" { value = oci_containerengine_cluster.main.endpoints[0].public_endpoint }
