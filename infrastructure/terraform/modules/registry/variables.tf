variable "compartment_ocid" { type = string }
variable "project_name" { type = string }
variable "freeform_tags" { type = map(string) }
variable "repositories" {
  description = "Container repository names to create (without the registry or namespace prefix)."
  type        = list(string)
}
