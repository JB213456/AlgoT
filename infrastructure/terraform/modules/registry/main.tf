# Private container repositories in OCI Container Registry (OCIR).
#
# OCIR replaces GitHub Container Registry for this deployment. Two reasons:
#
#  1. OCIR repositories are private by default and cannot be made public by
#     accident -- is_public is pinned false here and the free-tier gate
#     rejects any plan that sets it true. GHCR packages are created PUBLIC on
#     first push and have to be changed afterwards, which needs an
#     admin:packages token and is easy to forget.
#
#  2. The worker nodes reach OCIR over the service gateway, on Oracle's own
#     network, rather than egressing to the public internet for every pull.
#
# Cost: Container Registry is NOT enumerated on Oracle's Always Free
# resources page, and it is NOT free in this tenancy.
#
# An earlier note here claimed the "Container Image Storage" SKU billed
# GBP 0.00. That reading predated any image push. With the three AlgoT
# images published the SKU bills roughly GBP 0.0002/day -- about GBP 0.07 a
# year at ~1 GB. It is the only SKU in this tenancy still recording a
# non-zero recurring amount.
#
# The decision to accept that rather than move to GHCR, and the two
# alternatives considered, are recorded in docs/infrastructure/FREE-TIER.md
# ("Container Registry"). Keep that document as the single authority for the
# figures; do not restate them here.
#
# The charge scales with stored bytes, which is why
# deployment/scripts/registry-gc.sh reclaims superseded manifests and
# enforces a storage budget. scripts/check-costs.sh and the monitoring
# CronJob both report this SKU daily rather than at month end.

data "oci_objectstorage_namespace" "main" {
  compartment_id = var.compartment_ocid
}

resource "oci_artifacts_container_repository" "images" {
  for_each = toset(var.repositories)

  compartment_id = var.compartment_ocid
  display_name   = each.value

  # Never public. The gate rejects a plan that changes this.
  is_public = false

  # Tag immutability is NOT set here: the OCIR API in this region rejects it
  # with "Setting isImmutable is not currently supported". Immutability is
  # achieved the other way instead -- every manifest and deployment refers to
  # images by sha256 digest, never by tag, and deploy-data-platform.sh
  # refuses any reference without "@sha256:". Builds still use unique
  # arm64-<git-sha> tags rather than :latest.

  freeform_tags = var.freeform_tags
}
