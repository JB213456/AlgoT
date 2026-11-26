# Least-privilege IAM for OKE.
#
# The OKE service is granted only what managed node provisioning and the
# in-cluster CSI/cloud-controller need, scoped to the AlgoT compartment --
# never "manage all-resources", and never at tenancy scope.

resource "oci_identity_policy" "oke_service" {
  compartment_id = var.tenancy_ocid
  name           = "${var.project_name}-oke-service-policy"
  description    = "Scoped permissions for the OKE service in the ${var.compartment_name} compartment"
  freeform_tags  = var.freeform_tags

  statements = [
    # Managed node lifecycle.
    "Allow service oke to manage instance-family in compartment ${var.compartment_name}",
    # Attaching node VNICs to the private worker subnet.
    "Allow service oke to use subnets in compartment ${var.compartment_name}",
    "Allow service oke to use vnics in compartment ${var.compartment_name}",
    "Allow service oke to use network-security-groups in compartment ${var.compartment_name}",
    "Allow service oke to use private-ips in compartment ${var.compartment_name}",
    # Boot volumes for worker nodes, and block volumes the CSI driver attaches.
    "Allow service oke to manage volume-family in compartment ${var.compartment_name}",
    "Allow service oke to inspect compartments in compartment ${var.compartment_name}",
  ]
}

# Worker nodes authenticate as instance principals so the in-cluster CSI
# driver can attach the database volume and the backup CronJob can write to
# Object Storage -- without any static credential on the nodes.
resource "oci_identity_dynamic_group" "worker_nodes" {
  compartment_id = var.tenancy_ocid
  name           = "${var.project_name}-worker-nodes"
  description    = "AlgoT OKE worker node instances"
  freeform_tags  = var.freeform_tags

  matching_rule = "ALL {instance.compartment.id = '${var.compartment_ocid}'}"
}

resource "oci_identity_policy" "worker_nodes" {
  compartment_id = var.tenancy_ocid
  name           = "${var.project_name}-worker-nodes-policy"
  description    = "Block volume attachment and backup object writes for AlgoT worker nodes"
  freeform_tags  = var.freeform_tags

  statements = [
    "Allow dynamic-group ${oci_identity_dynamic_group.worker_nodes.name} to use volume-family in compartment ${var.compartment_name}",
    "Allow dynamic-group ${oci_identity_dynamic_group.worker_nodes.name} to use instance-family in compartment ${var.compartment_name}",
    "Allow dynamic-group ${oci_identity_dynamic_group.worker_nodes.name} to manage objects in compartment ${var.compartment_name} where target.bucket.name = '${var.project_name}-backups'",
    "Allow dynamic-group ${oci_identity_dynamic_group.worker_nodes.name} to read buckets in compartment ${var.compartment_name}",
    # Pull private images from OCIR over the service gateway, authenticated
    # as the node's instance principal rather than a stored registry token.
    "Allow dynamic-group ${oci_identity_dynamic_group.worker_nodes.name} to read repos in tenancy",
    # NOTE: deliberately NO "read secret-family" grant.
    #
    # The dynamic group matches *every instance in the compartment*, so any
    # pod that can reach the instance metadata endpoint inherits whatever the
    # nodes are allowed to do. Granting the nodes "read secret-family" put the
    # entire Vault -- including algot-cosign-private-key and
    # algot-ocir-auth-token -- one `oci secrets secret-bundle get` away from a
    # compromised container: read the cosign key and an attacker can sign
    # images the cluster will then accept, which turns a single container
    # escape into a supply-chain compromise.
    #
    # Nothing in-cluster needs it. Secret *values* reach pods as Kubernetes
    # Secrets, which deployment/scripts/vault-secrets.sh materialises from the
    # Vault using the *operator's* API key, outside the cluster. The only OCI
    # API the workloads themselves call is Object Storage (`oci os object`),
    # covered by the "manage objects" grant above.
  ]
}

# Object Storage's own service principal must be able to act on the bucket
# for lifecycle (expiry) rules to run. Without this, creating a lifecycle
# policy fails with InsufficientServicePermissions and backups would grow
# unbounded past the free 20 GB allowance.
resource "oci_identity_policy" "object_storage_lifecycle" {
  compartment_id = var.compartment_ocid
  name           = "${var.project_name}-objectstorage-lifecycle-policy"
  description    = "Lets Object Storage apply lifecycle expiry rules to the AlgoT backup bucket"
  freeform_tags  = var.freeform_tags

  statements = [
    # No bucket condition: service-principal statements do not reliably
    # support target.bucket.name, and an over-narrow statement fails closed
    # with InsufficientServicePermissions when the lifecycle rule is created.
    "Allow service objectstorage-${var.region} to manage object-family in compartment ${var.compartment_name}",
  ]
}

# Customer-managed-key encryption at rest for the market data volume and the
# Object Storage buckets requires the *service* principals to be able to use
# the key -- the caller's own permission is not enough. Without these grants,
# setting kms_key_id fails with a 404 on the key, and Object Storage silently
# cannot re-encrypt new objects.
#
# "use keys" grants Encrypt/Decrypt/GenerateDataEncryptionKey only. Neither
# service can create, rotate, disable or schedule deletion of the key.
resource "oci_identity_policy" "service_encryption" {
  compartment_id = var.compartment_ocid
  name           = "${var.project_name}-service-encryption-policy"
  description    = "Lets Block Volume and Object Storage use the AlgoT storage key for encryption at rest"
  freeform_tags  = var.freeform_tags

  statements = [
    "Allow service blockstorage to use keys in compartment ${var.compartment_name}",
    "Allow service objectstorage-${var.region} to use keys in compartment ${var.compartment_name}",
  ]
}
