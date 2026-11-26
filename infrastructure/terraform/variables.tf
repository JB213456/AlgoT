# ---------------------------------------------------------------------------
# Authentication / tenancy
# ---------------------------------------------------------------------------
variable "oci_config_file_profile" {
  description = "Profile in ~/.oci/config to authenticate with. Set to null in CI and supply tenancy_ocid/user_ocid/fingerprint/private_key_path instead."
  type        = string
  default     = "DEFAULT"
}

variable "tenancy_ocid" {
  description = "Tenancy OCID."
  type        = string
}

variable "compartment_ocid" {
  description = "Compartment that owns every AlgoT resource."
  type        = string
}

variable "user_ocid" {
  description = "User OCID (only used when oci_config_file_profile is null)."
  type        = string
  default     = null
}

variable "fingerprint" {
  description = "API key fingerprint (only used when oci_config_file_profile is null)."
  type        = string
  default     = null
}

variable "private_key_path" {
  description = "API private key path (only used when oci_config_file_profile is null)."
  type        = string
  default     = null
}

variable "region" {
  description = "OCI region. Must be the tenancy home region for this deployment."
  type        = string
  default     = "uk-london-1"
}

variable "availability_domain" {
  description = <<-EOT
    Single availability domain hosting both worker nodes.

    Both nodes share one AD on purpose: OCI block volumes are AD-scoped, so a
    same-AD pool lets the database's 80 GB volume re-attach to either node when
    one fails. Spreading nodes across ADs would permanently pin the database to
    whichever AD holds its volume.
  EOT
  type        = string
  default     = "IiKX:UK-LONDON-1-AD-1"
}

# ---------------------------------------------------------------------------
# Naming / tagging
# ---------------------------------------------------------------------------
variable "project_name" {
  description = "Name prefix for all resources."
  type        = string
  default     = "algot"
}

variable "freeform_tags" {
  description = "Tags applied to every taggable resource."
  type        = map(string)
  default = {
    project    = "algot"
    managed-by = "terraform"
    cost-tier  = "always-free"
  }
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------
variable "vcn_cidr" {
  description = "VCN CIDR block."
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(cidrnetmask(var.vcn_cidr))
    error_message = "vcn_cidr must be a valid IPv4 CIDR block."
  }
}

variable "k8s_api_subnet_cidr" {
  description = "Public subnet holding the Kubernetes API endpoint."
  type        = string
  default     = "10.0.0.0/28"
}

variable "workers_subnet_cidr" {
  description = "Private subnet holding the worker nodes."
  type        = string
  default     = "10.0.16.0/20"
}

variable "pods_subnet_cidr" {
  description = "VCN subnet from which pods draw real IP addresses (VCN-native pod networking)."
  type        = string
  default     = "10.0.32.0/19"
}

variable "services_cidr" {
  description = "Kubernetes service CIDR."
  type        = string
  default     = "10.96.0.0/16"
}

variable "api_allowed_cidrs" {
  description = <<-EOT
    CIDRs permitted to reach the Kubernetes API on 6443.

    Defaults to an empty list, which provisions NO public ingress rule; populate
    it with the operator's address (see docs/infrastructure/ACCESS.md). CI adds
    and removes its own runner address around a deployment rather than widening
    this permanently.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for c in var.api_allowed_cidrs : can(cidrnetmask(c))])
    error_message = "Every entry in api_allowed_cidrs must be a valid IPv4 CIDR block."
  }
}

# ---------------------------------------------------------------------------
# Kubernetes / compute -- Always Free envelope
# ---------------------------------------------------------------------------
variable "kubernetes_version" {
  description = "OKE control plane version."
  type        = string
  default     = "v1.34.10"
}

variable "node_image_id" {
  description = "OCID of the aarch64 OKE worker image. Resolved automatically when null."
  type        = string
  default     = null
}

variable "node_shape" {
  description = "Worker node shape. Must stay on the Always Free Ampere A1 shape."
  type        = string
  default     = "VM.Standard.A1.Flex"

  validation {
    condition     = var.node_shape == "VM.Standard.A1.Flex"
    error_message = "Only VM.Standard.A1.Flex is Always Free eligible. Any other shape bills immediately."
  }
}

variable "node_count" {
  description = "Number of worker nodes."
  type        = number
  default     = 2

  validation {
    condition     = var.node_count >= 1 && var.node_count <= 4
    error_message = "node_count must be between 1 and 4 to stay inside the 4 OCPU Always Free allowance."
  }
}

variable "node_ocpus" {
  description = "OCPUs per worker node."
  type        = number
  default     = 2
}

variable "node_memory_gbs" {
  description = "Memory (GB) per worker node."
  type        = number
  default     = 12
}

variable "node_boot_volume_gbs" {
  description = "Boot volume size (GB) per worker node. OCI's minimum is 50."
  type        = number
  default     = 50

  validation {
    condition     = var.node_boot_volume_gbs >= 50
    error_message = "OCI requires a minimum 50 GB boot volume."
  }
}

# ---------------------------------------------------------------------------
# Always Free ceilings -- enforced here AND by policies/free-tier
# ---------------------------------------------------------------------------
variable "free_tier_max_ocpus" {
  description = "Hard ceiling on total Ampere A1 OCPUs."
  type        = number
  default     = 4
}

variable "free_tier_max_memory_gbs" {
  description = "Hard ceiling on total Ampere A1 memory (GB)."
  type        = number
  default     = 24
}

variable "free_tier_max_storage_gbs" {
  description = "Hard ceiling on total block + boot storage (GB)."
  type        = number
  default     = 200
}

# ---------------------------------------------------------------------------
# Database storage
# ---------------------------------------------------------------------------
variable "database_volume_gbs" {
  description = "Persistent block volume (GB) for the market data database."
  type        = number
  default     = 80

  validation {
    condition     = var.database_volume_gbs >= 50
    error_message = "OCI's minimum block volume size is 50 GB."
  }
}

variable "state_bucket_name" {
  description = "Private, versioned bucket holding synced Terraform state."
  type        = string
  default     = "algot-terraform-state"
}

variable "backup_bucket_name" {
  description = "Private bucket holding database and ingestion-checkpoint backups."
  type        = string
  default     = "algot-backups"
}

variable "container_repositories" {
  description = "Private OCIR repositories to create."
  type        = list(string)
  default     = ["algot-database", "algot-ingestion", "algot-toolbox"]
}
