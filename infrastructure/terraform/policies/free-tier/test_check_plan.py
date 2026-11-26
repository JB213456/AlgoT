"""Tests for the Always Free compliance gate.

The gate's value is in what it REJECTS, so most of these assert rejection.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import check_plan  # noqa: E402

ALLOWLIST = pathlib.Path(__file__).parent / "allowlist.yaml"
POLICY = check_plan.load_allowlist(ALLOWLIST)


def plan(*changes: dict) -> dict:
    return {"resource_changes": list(changes)}


def change(rtype: str, after: dict, actions=("create",), address: str | None = None) -> dict:
    return {
        "type": rtype,
        "address": address or f"{rtype}.test",
        "change": {"actions": list(actions), "after": after, "before": after},
    }


def node_pool(size=2, ocpus=2, memory=12, boot=50, shape="VM.Standard.A1.Flex", cycling=False) -> dict:
    return change(
        "oci_containerengine_node_pool",
        {
            "node_shape": shape,
            "node_config_details": [{"size": size}],
            "node_shape_config": [{"ocpus": ocpus, "memory_in_gbs": memory}],
            "node_source_details": [{"boot_volume_size_in_gbs": boot}],
            "node_pool_cycling_details": [{"is_node_cycling_enabled": cycling}],
        },
    )


def cluster(ctype="BASIC_CLUSTER") -> dict:
    return change("oci_containerengine_cluster", {"type": ctype})


def volume(size=80, vpus=10, role="market-data-database") -> dict:
    return change(
        "oci_core_volume",
        {"size_in_gbs": size, "vpus_per_gb": vpus, "freeform_tags": {"algot-role": role}},
    )


def rules(violations) -> set[str]:
    return {v.rule for v in violations}


# --- the real, intended deployment passes -----------------------------------

def test_intended_deployment_passes():
    v, totals = check_plan.check(plan(cluster(), node_pool(), volume()), POLICY)
    assert v == [], [str(x) for x in v]
    assert totals["ocpus"] == 4
    assert totals["memory_gbs"] == 24
    assert totals["total_storage_gbs"] == 180


def test_reports_twenty_gb_storage_reserve():
    _, totals = check_plan.check(plan(cluster(), node_pool(), volume()), POLICY)
    assert POLICY["limits"]["max_total_storage_gbs"] - totals["total_storage_gbs"] == 20


# --- allocation ceilings -----------------------------------------------------

def test_rejects_ocpu_overallocation():
    v, _ = check_plan.check(plan(node_pool(size=3, ocpus=2)), POLICY)
    assert "ocpu-allowance" in rules(v)


def test_rejects_memory_overallocation():
    v, _ = check_plan.check(plan(node_pool(size=2, ocpus=2, memory=16)), POLICY)
    assert "memory-allowance" in rules(v)


def test_rejects_storage_overallocation():
    # The third 50 GB volume that OCI's minimum size would force.
    v, _ = check_plan.check(
        plan(node_pool(), volume(), volume(size=50, role="checkpoints")), POLICY
    )
    assert "storage-allowance" in rules(v)


def test_rejects_paid_shape():
    v, _ = check_plan.check(plan(node_pool(shape="VM.Standard.E4.Flex")), POLICY)
    assert "compute-shape" in rules(v)


def test_rejects_enhanced_cluster():
    v, _ = check_plan.check(plan(cluster("ENHANCED_CLUSTER")), POLICY)
    assert "cluster-type" in rules(v)


def test_rejects_second_cluster():
    v, _ = check_plan.check(plan(cluster(), change("oci_containerengine_cluster", {"type": "BASIC_CLUSTER"}, address="oci_containerengine_cluster.second")), POLICY)
    assert "cluster-count" in rules(v)


def test_rejects_high_performance_volume():
    v, _ = check_plan.check(plan(volume(vpus=20)), POLICY)
    assert "volume-performance" in rules(v)


def test_rejects_undersized_boot_volume():
    v, _ = check_plan.check(plan(node_pool(boot=40)), POLICY)
    assert "boot-volume-size" in rules(v)


def test_rejects_node_cycling():
    v, _ = check_plan.check(plan(node_pool(cycling=True)), POLICY)
    assert "node-cycling" in rules(v)


# --- fail-closed behaviour ---------------------------------------------------

def test_rejects_unknown_resource_type():
    v, _ = check_plan.check(plan(change("oci_some_new_service_thing", {})), POLICY)
    assert "unestablished-type" in rules(v)


def test_rejects_explicitly_denied_types():
    for rtype in ("oci_load_balancer_load_balancer", "oci_file_storage_file_system",
                  "oci_containerengine_virtual_node_pool", "oci_core_instance_pool",
                  "oci_database_autonomous_database"):
        v, _ = check_plan.check(plan(change(rtype, {})), POLICY)
        assert "denied-type" in rules(v), rtype


def test_rejects_indeterminate_node_sizing():
    v, _ = check_plan.check(
        plan(change("oci_containerengine_node_pool", {
            "node_shape": "VM.Standard.A1.Flex",
            "node_config_details": [{}],
            "node_shape_config": [{}],
            "node_source_details": [{"boot_volume_size_in_gbs": 50}],
        })),
        POLICY,
    )
    assert "indeterminate-sizing" in rules(v)


def test_rejects_public_backup_bucket():
    v, _ = check_plan.check(
        plan(change("oci_objectstorage_bucket", {"storage_tier": "Standard", "access_type": "ObjectRead"})),
        POLICY,
    )
    assert "bucket-exposure" in rules(v)


# --- data protection ---------------------------------------------------------

def test_rejects_destroying_database_volume():
    v, _ = check_plan.check(plan(volume() | {"change": {"actions": ["delete"], "before": {
        "size_in_gbs": 80, "freeform_tags": {"algot-role": "market-data-database"}}, "after": None}}), POLICY)
    assert "protected-resource" in rules(v)


def test_rejects_replacing_database_volume():
    v, _ = check_plan.check(
        plan({
            "type": "oci_core_volume",
            "address": "oci_core_volume.database",
            "change": {
                "actions": ["delete", "create"],
                "before": {"size_in_gbs": 80, "freeform_tags": {"algot-role": "market-data-database"}},
                "after": {"size_in_gbs": 100, "vpus_per_gb": 10,
                          "freeform_tags": {"algot-role": "market-data-database"}},
            },
        }),
        POLICY,
    )
    assert "protected-resource" in rules(v)


def test_rejects_destroying_cluster():
    v, _ = check_plan.check(
        plan({
            "type": "oci_containerengine_cluster",
            "address": "oci_containerengine_cluster.main",
            "change": {"actions": ["delete"], "before": {"type": "BASIC_CLUSTER"}, "after": None},
        }),
        POLICY,
    )
    assert "protected-resource" in rules(v)


def test_plain_deletion_of_unprotected_resource_is_allowed():
    v, _ = check_plan.check(
        plan({
            "type": "oci_core_route_table",
            "address": "oci_core_route_table.old",
            "change": {"actions": ["delete"], "before": {}, "after": None},
        }),
        POLICY,
    )
    assert v == []


# --- CLI contract ------------------------------------------------------------

def test_cli_exits_nonzero_on_violation(tmp_path):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan(node_pool(size=4))))
    assert check_plan.main([str(p), "--allowlist", str(ALLOWLIST)]) == 1


def test_cli_exits_zero_on_compliant_plan(tmp_path):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan(cluster(), node_pool(), volume())))
    assert check_plan.main([str(p), "--allowlist", str(ALLOWLIST)]) == 0


def test_cli_exits_two_on_unreadable_plan(tmp_path):
    assert check_plan.main([str(tmp_path / "nope.json"), "--allowlist", str(ALLOWLIST)]) == 2


def test_cli_exits_two_on_malformed_plan(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert check_plan.main([str(p), "--allowlist", str(ALLOWLIST)]) == 2


# --- the allowlist parser itself --------------------------------------------

def test_fallback_parser_matches_pyyaml():
    text = ALLOWLIST.read_text()
    fallback = check_plan._parse_minimal_yaml(text)
    yaml = pytest.importorskip("yaml")
    assert fallback == yaml.safe_load(text)


def test_cluster_destroy_allowed_only_with_explicit_flag():
    destroy = plan({
        "type": "oci_containerengine_cluster",
        "address": "oci_containerengine_cluster.main",
        "change": {"actions": ["delete", "create"],
                   "before": {"type": "BASIC_CLUSTER"},
                   "after": {"type": "BASIC_CLUSTER"}},
    })
    assert "protected-resource" in rules(check_plan.check(destroy, POLICY)[0])
    v, _ = check_plan.check(destroy, POLICY, allow_cluster_replacement=True)
    assert "protected-resource" not in rules(v)


def _node_pool_replacement() -> dict:
    """A plan that replaces the node pool, as a forced-replacement attribute
    change produces."""
    after = node_pool()["change"]["after"]
    return plan({
        "type": "oci_containerengine_node_pool",
        "address": "module.compute.oci_containerengine_node_pool.main",
        "change": {"actions": ["delete", "create"], "before": after, "after": after},
    })


def test_rejects_replacing_the_node_pool():
    # Replacing the pool recycles both workers at once, and 4 OCPUs leaves no
    # room for a surge node, so there is no rolling path.
    v, _ = check_plan.check(_node_pool_replacement(), POLICY)
    assert "protected-resource" in rules(v)


def test_rejects_destroying_the_node_pool():
    v, _ = check_plan.check(
        plan({
            "type": "oci_containerengine_node_pool",
            "address": "module.compute.oci_containerengine_node_pool.main",
            "change": {"actions": ["delete"],
                       "before": node_pool()["change"]["after"],
                       "after": None},
        }),
        POLICY,
    )
    assert "protected-resource" in rules(v)


def test_node_pool_replacement_allowed_only_with_explicit_flag():
    v, _ = check_plan.check(
        _node_pool_replacement(), POLICY, allow_node_pool_replacement=True
    )
    assert "protected-resource" not in rules(v)


def test_node_pool_flag_does_not_unprotect_the_cluster_or_the_database():
    # Each override is scoped to the one resource it names.
    v, _ = check_plan.check(
        plan({
            "type": "oci_containerengine_cluster",
            "address": "oci_containerengine_cluster.main",
            "change": {"actions": ["delete"], "before": {"type": "BASIC_CLUSTER"}, "after": None},
        }),
        POLICY,
        allow_node_pool_replacement=True,
    )
    assert "protected-resource" in rules(v)

    v, _ = check_plan.check(
        plan({
            "type": "oci_core_volume",
            "address": "oci_core_volume.database",
            "change": {"actions": ["delete"],
                       "before": {"freeform_tags": {"algot-role": "market-data-database"}},
                       "after": None},
        }),
        POLICY,
        allow_node_pool_replacement=True,
    )
    assert "protected-resource" in rules(v)


def test_cluster_flag_does_not_unprotect_the_node_pool():
    v, _ = check_plan.check(
        _node_pool_replacement(), POLICY, allow_cluster_replacement=True
    )
    assert "protected-resource" in rules(v)


def test_in_place_node_pool_update_is_not_treated_as_replacement():
    # The encryption-at-rest and in-transit changes are in-place updates and
    # must not trip the guard.
    after = node_pool()["change"]["after"]
    after["node_config_details"][0]["is_pv_encryption_in_transit_enabled"] = True
    v, _ = check_plan.check(
        plan({
            "type": "oci_containerengine_node_pool",
            "address": "module.compute.oci_containerengine_node_pool.main",
            "change": {"actions": ["update"], "before": after, "after": after},
        }),
        POLICY,
    )
    assert "protected-resource" not in rules(v)


def test_flag_does_not_also_unprotect_the_database_volume():
    # The override exists for CNI changes; it must not become a blanket
    # permission to destroy market data.
    v, _ = check_plan.check(
        plan({
            "type": "oci_core_volume",
            "address": "oci_core_volume.database",
            "change": {"actions": ["delete"],
                       "before": {"freeform_tags": {"algot-role": "market-data-database"}},
                       "after": None},
        }),
        POLICY,
        allow_cluster_replacement=True,
    )
    assert "protected-resource" in rules(v)


# --- registry and vault (private images, secrets held in Oracle) -------------

def test_accepts_private_repository_and_free_vault():
    v, _ = check_plan.check(
        plan(
            change("oci_artifacts_container_repository", {"is_public": False, "is_immutable": True}),
            change("oci_kms_vault", {"vault_type": "DEFAULT"}),
            change("oci_kms_key", {"protection_mode": "SOFTWARE"}),
        ),
        POLICY,
    )
    assert v == [], [str(x) for x in v]


def test_rejects_public_container_repository():
    v, _ = check_plan.check(
        plan(change("oci_artifacts_container_repository", {"is_public": True})), POLICY
    )
    assert "registry-exposure" in rules(v)


def test_rejects_virtual_private_vault():
    v, _ = check_plan.check(
        plan(change("oci_kms_vault", {"vault_type": "VIRTUAL_PRIVATE"})), POLICY
    )
    assert "vault-type" in rules(v)


def test_rejects_hsm_protected_key():
    v, _ = check_plan.check(
        plan(change("oci_kms_key", {"protection_mode": "HSM"})), POLICY
    )
    assert "key-protection-mode" in rules(v)


def test_rejects_dedicated_hsm_cluster():
    v, _ = check_plan.check(plan(change("oci_kms_hsm_cluster", {})), POLICY)
    assert "denied-type" in rules(v)


# --- totals must reflect the whole planned state, not just the diff --------

def planned_state(*resources: dict) -> dict:
    return {"planned_values": {"root_module": {"child_modules": [
        {"address": "module.x", "resources": list(resources)}]}}}


def test_totals_come_from_planned_state_not_just_changes():
    # An incremental plan that only adds a bucket must still report the
    # cluster's real allocation, or the allowance check is meaningless.
    doc = {
        "resource_changes": [change("oci_objectstorage_bucket", {"storage_tier": "Standard"})],
        **planned_state(
            {"address": "module.x.np", "type": "oci_containerengine_node_pool", "values": {
                "node_shape": "VM.Standard.A1.Flex",
                "node_config_details": [{"size": 2}],
                "node_shape_config": [{"ocpus": 2, "memory_in_gbs": 12}],
                "node_source_details": [{"boot_volume_size_in_gbs": 50}],
            }},
            {"address": "module.x.vol", "type": "oci_core_volume", "values": {"size_in_gbs": 80}},
        ),
    }
    v, totals = check_plan.check(doc, POLICY)
    assert totals["ocpus"] == 4
    assert totals["memory_gbs"] == 24
    assert totals["total_storage_gbs"] == 180
    assert v == [], [str(x) for x in v]


def test_detects_preexisting_overallocation_in_planned_state():
    # A plan whose diff is harmless but whose resulting state is over the
    # allowance must still be rejected.
    doc = {
        "resource_changes": [change("oci_objectstorage_bucket", {"storage_tier": "Standard"})],
        **planned_state(
            {"address": "module.x.np", "type": "oci_containerengine_node_pool", "values": {
                "node_shape": "VM.Standard.A1.Flex",
                "node_config_details": [{"size": 4}],
                "node_shape_config": [{"ocpus": 2, "memory_in_gbs": 12}],
                "node_source_details": [{"boot_volume_size_in_gbs": 50}],
            }},
        ),
    }
    v, totals = check_plan.check(doc, POLICY)
    assert totals["ocpus"] == 8
    assert "ocpu-allowance" in rules(v)
    assert "memory-allowance" in rules(v)


def test_inert_surge_values_are_allowed_while_cycling_is_disabled():
    # OCI requires maximum_surge/maximum_unavailable to be 1-4 once the pool
    # exists, even with cycling off. Those values are inert, so the gate must
    # not reject them -- only actual enablement.
    doc = plan(change("oci_containerengine_node_pool", {
        "node_shape": "VM.Standard.A1.Flex",
        "node_config_details": [{"size": 2}],
        "node_shape_config": [{"ocpus": 2, "memory_in_gbs": 12}],
        "node_source_details": [{"boot_volume_size_in_gbs": 50}],
        "node_pool_cycling_details": [{
            "is_node_cycling_enabled": False,
            "maximum_surge": 1,
            "maximum_unavailable": 1,
        }],
    }))
    v, _ = check_plan.check(doc, POLICY)
    assert "node-cycling" not in rules(v), [str(x) for x in v]
