output "registry_endpoint" {
  description = "OCIR endpoint for this region."
  value       = "lhr.ocir.io"
}

output "registry_namespace" {
  description = "Tenancy object storage namespace, which is also the OCIR namespace."
  value       = data.oci_objectstorage_namespace.main.namespace
}

output "repository_urls" {
  description = "Fully qualified image paths, without a tag."
  value = {
    for name, repo in oci_artifacts_container_repository.images :
    name => "lhr.ocir.io/${data.oci_objectstorage_namespace.main.namespace}/${name}"
  }
}

output "billable_size_in_gbs" {
  description = "Billable storage per repository. Watched against the free allowance."
  value = {
    for name, repo in oci_artifacts_container_repository.images :
    name => repo.billable_size_in_gbs
  }
}
