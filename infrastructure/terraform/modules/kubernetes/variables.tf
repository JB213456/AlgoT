variable "compartment_ocid" { type = string }
variable "project_name" { type = string }
variable "freeform_tags" { type = map(string) }
variable "vcn_id" { type = string }
variable "k8s_api_subnet_id" { type = string }
variable "k8s_api_nsg_id" { type = string }
variable "kubernetes_version" { type = string }
variable "services_cidr" { type = string }
