variable "compartment_ocid" { type = string }
variable "project_name" { type = string }
variable "freeform_tags" { type = map(string) }
variable "cluster_id" { type = string }
variable "kubernetes_version" { type = string }
variable "availability_domain" { type = string }
variable "workers_subnet_id" { type = string }
variable "workers_nsg_id" { type = string }
variable "node_shape" { type = string }
variable "node_count" { type = number }
variable "node_ocpus" { type = number }
variable "node_memory_gbs" { type = number }
variable "node_boot_volume_gbs" { type = number }
variable "node_image_id" {
  type    = string
  default = null
}
variable "free_tier_max_ocpus" { type = number }
variable "free_tier_max_memory_gbs" { type = number }
variable "pods_subnet_id" { type = string }
variable "pods_nsg_id" { type = string }
variable "max_pods_per_node" {
  type    = number
  default = 31
}
