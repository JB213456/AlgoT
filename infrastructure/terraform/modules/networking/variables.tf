variable "compartment_ocid" { type = string }
variable "project_name" { type = string }
variable "freeform_tags" { type = map(string) }
variable "vcn_cidr" { type = string }
variable "k8s_api_subnet_cidr" { type = string }
variable "workers_subnet_cidr" { type = string }
variable "pods_subnet_cidr" { type = string }
