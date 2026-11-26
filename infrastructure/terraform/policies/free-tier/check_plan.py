#!/usr/bin/env python3
"""Reject any Terraform plan that is not provably Always Free eligible.

Reads `terraform show -json <plan>` output and validates it against
allowlist.yaml. Fails closed: a resource type whose free eligibility has not
been established is a violation, not a warning.

Usage:
    terraform show -json plan.tfplan > plan.json
    python3 check_plan.py plan.json [--allowlist allowlist.yaml]

Exit codes:
    0  every resource is established as Always Free eligible
    1  one or more violations
    2  the plan could not be read or parsed
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

# Actions that result in a resource existing after apply. "delete" alone costs
# nothing, so it is not evaluated for eligibility.
CREATING_ACTIONS = {"create", "update", "read"}

DESTRUCTIVE_ACTIONS = {"delete"}

# Resources whose destruction or replacement would lose market data or break
# the single-cluster allowance.
PROTECTED_ROLES = {
    "market-data-database": "the market data block volume",
}


class Violation:
    def __init__(self, address: str, rule: str, detail: str) -> None:
        self.address = address
        self.rule = rule
        self.detail = detail

    def __str__(self) -> str:
        return f"  [{self.rule}] {self.address}\n      {self.detail}"


def load_allowlist(path: pathlib.Path) -> dict:
    text = path.read_text()
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except ImportError:
        return _parse_minimal_yaml(text)


def _parse_minimal_yaml(text: str) -> dict:
    """Parse the restricted YAML subset allowlist.yaml uses.

    Avoids making PyYAML a hard dependency of the CI gate. Handles nested
    mappings and lists of scalars, which is all this file needs. Verified
    against PyYAML by test_fallback_parser_matches_pyyaml.
    """
    lines = [
        (len(raw) - len(raw.lstrip()), raw.strip())
        for raw in text.splitlines()
        if raw.strip() and not raw.lstrip().startswith("#")
    ]

    def parse_block(index: int, indent: int) -> tuple[dict, int]:
        node: dict = {}
        while index < len(lines):
            line_indent, line = lines[index]
            if line_indent < indent:
                break
            if line.startswith("- "):
                raise ValueError(f"unexpected list item at line {index + 1}")

            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()

            if value:
                node[key] = _scalar(value)
                index += 1
                continue

            # Empty value: the child is whatever the next deeper line is.
            if index + 1 < len(lines) and lines[index + 1][0] > line_indent:
                child_indent, child_line = lines[index + 1]
                if child_line.startswith("- "):
                    items = []
                    index += 1
                    while index < len(lines) and lines[index][0] == child_indent and lines[index][1].startswith("- "):
                        items.append(_scalar(lines[index][1][2:]))
                        index += 1
                    node[key] = items
                else:
                    node[key], index = parse_block(index + 1, child_indent)
            else:
                node[key] = {}
                index += 1
        return node, index

    root, _ = parse_block(0, 0)
    return root


def _scalar(value: str):
    value = value.strip().strip('"').strip("'")
    try:
        return int(value)
    except ValueError:
        return value


def _first(block, key, default=None):
    """Read `key` from a Terraform nested block, which is encoded as a list."""
    value = block.get(key)
    if isinstance(value, list):
        if not value:
            return default
        value = value[0]
    if isinstance(value, dict):
        return value
    return value if value is not None else default


def iter_planned_resources(plan: dict):
    """Yield (address, type, values) for every resource in the planned state.

    `resource_changes` only lists resources this plan modifies, so totals
    derived from it under-report on an incremental plan -- an apply that adds
    one bucket would appear to use 0 OCPUs. `planned_values` is the full
    desired state after apply, which is what the allowance must be measured
    against.
    """
    def walk(module):
        for res in module.get("resources", []) or []:
            yield res.get("address", res.get("type", "")), res.get("type", ""), res.get("values") or {}
        for child in module.get("child_modules", []) or []:
            yield from walk(child)

    root = (plan.get("planned_values") or {}).get("root_module") or {}
    yield from walk(root)


def check(
    plan: dict,
    policy: dict,
    allow_cluster_replacement: bool = False,
    allow_node_pool_replacement: bool = False,
) -> tuple[list[Violation], dict]:
    allowed = policy.get("allowed", {}) or {}
    denied = policy.get("denied", {}) or {}
    limits = policy.get("limits", {}) or {}

    violations: list[Violation] = []
    totals = {
        "ocpus": 0.0,
        "memory_gbs": 0.0,
        "boot_storage_gbs": 0.0,
        "block_storage_gbs": 0.0,
        "node_count": 0,
        "clusters": 0,
    }

    for change in plan.get("resource_changes", []):
        rtype = change.get("type", "")
        address = change.get("address", rtype)
        actions = set(change.get("change", {}).get("actions", []))
        after = change.get("change", {}).get("before") if actions == DESTRUCTIVE_ACTIONS else change.get("change", {}).get("after")
        after = after or {}

        # Flag destruction or replacement of protected data separately: it is
        # not a billing problem but it is a data-loss problem.
        if "delete" in actions:
            tags = (after.get("freeform_tags") or {}) if isinstance(after, dict) else {}
            role = tags.get("algot-role")
            if role in PROTECTED_ROLES:
                verb = "replace" if "create" in actions else "destroy"
                violations.append(
                    Violation(
                        address,
                        "protected-resource",
                        f"Plan would {verb} {PROTECTED_ROLES[role]}. Refusing.",
                    )
                )
            if rtype == "oci_containerengine_cluster" and not allow_cluster_replacement:
                verb = "replace" if "create" in actions else "destroy"
                violations.append(
                    Violation(
                        address,
                        "protected-resource",
                        f"Plan would {verb} the OKE cluster. The tenancy allows only one cluster, so "
                        "there is no blue/green path and the data plane goes with it. If this is "
                        "intended, re-run with --allow-cluster-replacement.",
                    )
                )

            # Replacing the node pool recycles BOTH workers at once. There is
            # no spare OCPU headroom to stand up a replacement pool
            # alongside -- 2 nodes already consume the whole 4 OCPU
            # allowance -- so the data plane is simply gone until the new
            # nodes join, the database volume is force-detached, and the pods
            # have nowhere to be rescheduled. Most node pool attributes are
            # updatable in place; a plan that replaces the pool is nearly
            # always an accident.
            if rtype == "oci_containerengine_node_pool" and not allow_node_pool_replacement:
                verb = "replace" if "create" in actions else "destroy"
                violations.append(
                    Violation(
                        address,
                        "protected-resource",
                        f"Plan would {verb} the worker node pool, recycling both Ampere A1 nodes at "
                        "once. The 4 OCPU allowance leaves no room for a surge node, so this is an "
                        "outage, not a rolling change. Check whether the attribute that forces "
                        "replacement can be changed in place instead. If the recycle is intended, "
                        "re-run with --allow-node-pool-replacement.",
                    )
                )

        if not actions & CREATING_ACTIONS:
            continue

        if rtype in denied:
            violations.append(Violation(address, "denied-type", denied[rtype]))
            continue

        if rtype not in allowed:
            violations.append(
                Violation(
                    address,
                    "unestablished-type",
                    f"Resource type '{rtype}' is not in the free-tier allowlist. "
                    "Free eligibility must be established and recorded in allowlist.yaml before it can be provisioned.",
                )
            )
            continue

        if rtype == "oci_containerengine_cluster":
            totals["clusters"] += 1
            ctype = after.get("type")
            if ctype not in (limits.get("allowed_cluster_types") or []):
                violations.append(
                    Violation(
                        address,
                        "cluster-type",
                        f"Cluster type is {ctype!r}; only {limits.get('allowed_cluster_types')} is free. "
                        "Enhanced clusters bill per cluster-hour.",
                    )
                )

        elif rtype == "oci_containerengine_node_pool":
            shape = after.get("node_shape")
            if shape not in (limits.get("allowed_compute_shapes") or []):
                violations.append(
                    Violation(
                        address,
                        "compute-shape",
                        f"Node shape {shape!r} is not Always Free eligible. "
                        f"Only {limits.get('allowed_compute_shapes')} qualifies.",
                    )
                )

            cfg = _first(after, "node_config_details", {}) or {}
            size = cfg.get("size")
            shape_cfg = _first(after, "node_shape_config", {}) or {}
            ocpus = shape_cfg.get("ocpus")
            memory = shape_cfg.get("memory_in_gbs")
            src = _first(after, "node_source_details", {}) or {}
            boot = src.get("boot_volume_size_in_gbs")

            if size is None or ocpus is None or memory is None:
                violations.append(
                    Violation(
                        address,
                        "indeterminate-sizing",
                        "Node pool size, OCPUs or memory is unknown at plan time, so the "
                        "free-tier allocation cannot be established. Set them explicitly.",
                    )
                )
            else:
                totals["node_count"] += int(size)
                totals["ocpus"] += float(ocpus) * int(size)
                totals["memory_gbs"] += float(memory) * int(size)

            if boot is None:
                violations.append(
                    Violation(address, "indeterminate-sizing", "Boot volume size is unknown at plan time.")
                )
            else:
                boot = float(boot)
                min_boot = float(limits.get("min_boot_volume_gbs", 50))
                if boot < min_boot:
                    violations.append(
                        Violation(
                            address,
                            "boot-volume-size",
                            f"Boot volume {boot} GB is below OCI's {min_boot} GB minimum.",
                        )
                    )
                if size is not None:
                    totals["boot_storage_gbs"] += boot * int(size)

            # Autoscaling past the free allowance must be impossible.
            if after.get("node_pool_cycling_details") and _first(after, "node_pool_cycling_details", {}).get(
                "is_node_cycling_enabled"
            ):
                violations.append(
                    Violation(
                        address,
                        "node-cycling",
                        "Node cycling temporarily doubles the node count, which would exceed "
                        "the 4 OCPU allowance mid-cycle.",
                    )
                )

        elif rtype == "oci_core_volume":
            size = after.get("size_in_gbs")
            vpus = after.get("vpus_per_gb")
            if size is None:
                violations.append(
                    Violation(address, "indeterminate-sizing", "Block volume size is unknown at plan time.")
                )
            else:
                totals["block_storage_gbs"] += float(size)
            max_vpus = float(limits.get("max_block_volume_vpus_per_gb", 10))
            if vpus is not None and float(vpus) > max_vpus:
                violations.append(
                    Violation(
                        address,
                        "volume-performance",
                        f"vpus_per_gb={vpus} exceeds {max_vpus}. Higher performance tiers bill "
                        "Block Volume Performance Units.",
                    )
                )

        elif rtype == "oci_artifacts_container_repository":
            # A public registry defeats the point of moving off GHCR, and
            # public pulls are metered differently.
            if after.get("is_public") is True:
                violations.append(
                    Violation(
                        address,
                        "registry-exposure",
                        "Container repository is public. AlgoT images must be private; "
                        "that is the reason this deployment uses OCIR rather than GHCR.",
                    )
                )

        elif rtype == "oci_kms_vault":
            vtype = after.get("vault_type")
            allowed_vaults = limits.get("allowed_vault_types") or []
            if vtype is not None and vtype not in allowed_vaults:
                violations.append(
                    Violation(
                        address,
                        "vault-type",
                        f"Vault type {vtype!r} is not free. Only {allowed_vaults} is included in "
                        "Always Free; a virtual private vault bills per hour.",
                    )
                )

        elif rtype == "oci_kms_key":
            mode = after.get("protection_mode")
            allowed_modes = limits.get("allowed_key_protection_modes") or []
            if mode is not None and mode not in allowed_modes:
                violations.append(
                    Violation(
                        address,
                        "key-protection-mode",
                        f"Key protection mode {mode!r} is not free for unlimited versions. "
                        f"Only {allowed_modes} is; HSM keys are capped at 20 free versions "
                        "tenancy-wide and bill beyond that.",
                    )
                )

        elif rtype == "oci_objectstorage_bucket":
            tier = after.get("storage_tier")
            if tier is not None and tier not in (limits.get("allowed_bucket_tiers") or []):
                violations.append(
                    Violation(
                        address,
                        "bucket-tier",
                        f"Bucket storage tier {tier!r} is not in the free allowance.",
                    )
                )
            if after.get("access_type") not in (None, "NoPublicAccess"):
                violations.append(
                    Violation(
                        address,
                        "bucket-exposure",
                        f"Bucket access_type is {after.get('access_type')!r}; backups must not be public.",
                    )
                )

    # Recompute allocation totals from the full planned state so an
    # incremental plan is still measured against the real allowance.
    planned = list(iter_planned_resources(plan))
    if planned:
        totals.update(ocpus=0.0, memory_gbs=0.0, boot_storage_gbs=0.0,
                      block_storage_gbs=0.0, node_count=0, clusters=0)
        for address, rtype, values in planned:
            if rtype == "oci_containerengine_cluster":
                totals["clusters"] += 1
            elif rtype == "oci_containerengine_node_pool":
                cfg = _first(values, "node_config_details", {}) or {}
                shape_cfg = _first(values, "node_shape_config", {}) or {}
                src = _first(values, "node_source_details", {}) or {}
                size = cfg.get("size")
                ocpus = shape_cfg.get("ocpus")
                memory = shape_cfg.get("memory_in_gbs")
                boot = src.get("boot_volume_size_in_gbs")
                if size is not None:
                    totals["node_count"] += int(size)
                    if ocpus is not None:
                        totals["ocpus"] += float(ocpus) * int(size)
                    if memory is not None:
                        totals["memory_gbs"] += float(memory) * int(size)
                    if boot is not None:
                        totals["boot_storage_gbs"] += float(boot) * int(size)
            elif rtype == "oci_core_volume":
                size = values.get("size_in_gbs")
                if size is not None:
                    totals["block_storage_gbs"] += float(size)

    max_ocpus = float(limits.get("max_total_ocpus", 4))
    max_mem = float(limits.get("max_total_memory_gbs", 24))
    max_storage = float(limits.get("max_total_storage_gbs", 200))
    total_storage = totals["boot_storage_gbs"] + totals["block_storage_gbs"]
    totals["total_storage_gbs"] = total_storage

    if totals["ocpus"] > max_ocpus:
        violations.append(
            Violation("<plan total>", "ocpu-allowance", f"Total {totals['ocpus']} OCPUs exceeds the {max_ocpus} OCPU allowance.")
        )
    if totals["memory_gbs"] > max_mem:
        violations.append(
            Violation("<plan total>", "memory-allowance", f"Total {totals['memory_gbs']} GB memory exceeds the {max_mem} GB allowance.")
        )
    if total_storage > max_storage:
        violations.append(
            Violation(
                "<plan total>",
                "storage-allowance",
                f"Total {total_storage} GB storage ({totals['boot_storage_gbs']} GB boot + "
                f"{totals['block_storage_gbs']} GB block) exceeds the {max_storage} GB allowance.",
            )
        )
    if totals["clusters"] > 1:
        violations.append(
            Violation("<plan total>", "cluster-count", f"Plan creates {totals['clusters']} clusters; the tenancy limit is 1.")
        )

    return violations, totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Always Free compliance gate for Terraform plans.")
    parser.add_argument("plan_json", help="Output of `terraform show -json <planfile>`")
    parser.add_argument(
        "--allow-cluster-replacement",
        action="store_true",
        help="Permit a plan that destroys or replaces the OKE cluster. Required for a CNI "
             "change, which cannot be done in place. The database volume is a separate "
             "resource and is never affected.",
    )
    parser.add_argument(
        "--allow-node-pool-replacement",
        action="store_true",
        help="Permit a plan that destroys or replaces the worker node pool. Recycles both "
             "Ampere A1 nodes at once, with no surge capacity inside the 4 OCPU allowance.",
    )
    parser.add_argument(
        "--allowlist",
        default=str(pathlib.Path(__file__).parent / "allowlist.yaml"),
        help="Path to allowlist.yaml",
    )
    args = parser.parse_args(argv)

    try:
        plan = json.loads(pathlib.Path(args.plan_json).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: could not read plan JSON: {exc}", file=sys.stderr)
        return 2

    try:
        policy = load_allowlist(pathlib.Path(args.allowlist))
    except (OSError, ValueError) as exc:
        print(f"FAIL: could not read allowlist: {exc}", file=sys.stderr)
        return 2

    violations, totals = check(
        plan,
        policy,
        allow_cluster_replacement=args.allow_cluster_replacement,
        allow_node_pool_replacement=args.allow_node_pool_replacement,
    )

    if args.allow_cluster_replacement:
        print("NOTE: --allow-cluster-replacement was passed; cluster destruction is permitted.")
    if args.allow_node_pool_replacement:
        print("NOTE: --allow-node-pool-replacement was passed; recycling both workers is permitted.")

    print("Always Free compliance report")
    print("=" * 60)
    print(f"  Worker nodes          : {totals['node_count']}")
    print(f"  Total OCPUs           : {totals['ocpus']:g} / {policy['limits']['max_total_ocpus']}")
    print(f"  Total memory (GB)     : {totals['memory_gbs']:g} / {policy['limits']['max_total_memory_gbs']}")
    print(f"  Boot storage (GB)     : {totals['boot_storage_gbs']:g}")
    print(f"  Block storage (GB)    : {totals['block_storage_gbs']:g}")
    print(f"  Total storage (GB)    : {totals['total_storage_gbs']:g} / {policy['limits']['max_total_storage_gbs']}")
    print("=" * 60)

    if violations:
        print(f"\nREJECTED: {len(violations)} free-tier violation(s)\n")
        for v in violations:
            print(v)
        print("\nNo resources were applied. Resolve every violation before applying.")
        return 1

    print("\nPASS: every resource in this plan is established as Always Free eligible.")
    print("Projected recurring cost: GBP 0.00")
    return 0


if __name__ == "__main__":
    sys.exit(main())
