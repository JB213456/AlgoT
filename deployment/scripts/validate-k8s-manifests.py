#!/usr/bin/env python3
"""Assert the cost and security invariants of the AlgoT Kubernetes manifests.

Schema validation proves a manifest is well formed. These checks prove it is
*correct for this deployment*:

  - no Service type that provisions a billable OCI load balancer
  - no mutable image tags, so rollbacks are meaningful
  - Pod Security Standards "restricted" is satisfiable
  - every container declares CPU and memory requests and limits
  - the database's PersistentVolume retains its data
  - no workload runs as root or escalates privileges
  - every namespaced object targets algot-data and nothing else

Usage:  validate-k8s-manifests.py <dir> [<dir> ...]
Exit:   0 clean, 1 violations found, 2 could not read manifests
"""
from __future__ import annotations

import pathlib
import sys

try:
    import yaml
except ImportError:
    sys.exit("FATAL: PyYAML is required (pip install pyyaml)")

# A Service of these types makes OCI provision a load balancer, which bills.
BILLABLE_SERVICE_TYPES = {"LoadBalancer", "NodePort"}

PLACEHOLDER_PREFIX = "__"

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod", "ReplicaSet"}

# The only namespace this repository may deploy workloads into. An object
# that omits `metadata.namespace` lands in whatever namespace kubectl's
# current context points at, so silence is a violation too, not a default.
TARGET_NAMESPACE = "algot-data"

# Kinds that have no namespace by definition. Anything not listed here is
# treated as namespaced and must name the target namespace explicitly.
CLUSTER_SCOPED_KINDS = {
    "APIService",
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "IngressClass",
    "MutatingWebhookConfiguration",
    "Namespace",
    "Node",
    "PersistentVolume",
    "PriorityClass",
    "RuntimeClass",
    "StorageClass",
    "ValidatingAdmissionPolicy",
    "ValidatingAdmissionPolicyBinding",
    "ValidatingWebhookConfiguration",
    # Calico's policy kinds, used by the network policy manifests.
    "GlobalNetworkPolicy",
    "GlobalNetworkSet",
}


class Problem:
    def __init__(self, path: str, obj: str, message: str) -> None:
        self.path, self.obj, self.message = path, obj, message

    def __str__(self) -> str:
        return f"  {self.path} [{self.obj}]\n      {self.message}"


def pod_specs(doc: dict):
    """Yield (descriptor, podSpec) for every pod template in a document."""
    kind = doc.get("kind")
    if kind == "Pod":
        yield "spec", doc.get("spec", {})
    elif kind == "CronJob":
        spec = doc.get("spec", {}).get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
        if spec:
            yield "spec.jobTemplate.spec.template.spec", spec
    elif kind in WORKLOAD_KINDS:
        spec = doc.get("spec", {}).get("template", {}).get("spec", {})
        if spec:
            yield "spec.template.spec", spec


def all_containers(pod_spec: dict):
    """Yield (section, container). initContainers includes native sidecars."""
    for section in ("initContainers", "containers"):
        for c in pod_spec.get(section) or []:
            yield section, c


def check_document(doc: dict, path: str, problems: list[Problem]) -> None:
    kind = doc.get("kind", "?")
    name = doc.get("metadata", {}).get("name", "?")
    obj = f"{kind}/{name}"

    # --- scope: only the algot-data namespace may be deployed into ---
    namespace = (doc.get("metadata") or {}).get("namespace")
    if kind == "Namespace":
        if name != TARGET_NAMESPACE:
            problems.append(Problem(path, obj, f"this repository may only create the {TARGET_NAMESPACE} namespace, not {name!r}."))
    elif kind not in CLUSTER_SCOPED_KINDS:
        if namespace is None:
            problems.append(Problem(path, obj, f"no metadata.namespace, so this object would land in whichever namespace the caller's context selects. Set namespace: {TARGET_NAMESPACE}."))
        elif namespace != TARGET_NAMESPACE:
            problems.append(Problem(path, obj, f"targets namespace {namespace!r}; only {TARGET_NAMESPACE} may be deployed into."))
    elif namespace is not None:
        problems.append(Problem(path, obj, f"{kind} is cluster-scoped but declares metadata.namespace={namespace!r}, which is misleading and ignored by the API server."))

    # --- cost: no billable Service types ---
    if kind == "Service":
        svc_type = doc.get("spec", {}).get("type", "ClusterIP")
        if svc_type in BILLABLE_SERVICE_TYPES:
            problems.append(Problem(path, obj, f"Service type {svc_type} provisions a billable OCI load balancer. Use ClusterIP."))

    if kind == "Ingress":
        problems.append(Problem(path, obj, "An Ingress requires a billable load balancer and would expose the data tier."))

    # --- cost: quota must forbid load balancers outright ---
    if kind == "ResourceQuota":
        hard = doc.get("spec", {}).get("hard", {}) or {}
        for key in ("services.loadbalancers", "services.nodeports"):
            if str(hard.get(key, "")) != "0":
                problems.append(Problem(path, obj, f"ResourceQuota should pin {key} to 0 so a billable Service cannot be created."))

    # --- data protection ---
    if kind == "PersistentVolume":
        policy = doc.get("spec", {}).get("persistentVolumeReclaimPolicy")
        if policy != "Retain":
            problems.append(Problem(path, obj, f"reclaimPolicy is {policy!r}; the market data volume must be Retain."))

    if kind == "StorageClass":
        if doc.get("reclaimPolicy") != "Retain":
            problems.append(Problem(path, obj, "StorageClass reclaimPolicy must be Retain so deleting a PVC cannot delete market data."))

    # --- namespace must enforce restricted Pod Security Standards ---
    if kind == "Namespace":
        labels = doc.get("metadata", {}).get("labels", {}) or {}
        if labels.get("pod-security.kubernetes.io/enforce") != "restricted":
            problems.append(Problem(path, obj, "Namespace must set pod-security.kubernetes.io/enforce=restricted."))

    # --- workloads ---
    for descriptor, spec in pod_specs(doc):
        sc = spec.get("securityContext", {}) or {}
        if sc.get("runAsNonRoot") is not True:
            problems.append(Problem(path, obj, f"{descriptor}.securityContext.runAsNonRoot must be true."))

        containers = list(all_containers(spec))
        if not containers:
            problems.append(Problem(path, obj, f"{descriptor} declares no containers."))

        for section, c in containers:
            cname = c.get("name", "?")
            where = f"{descriptor}.{section}[{cname}]"

            image = c.get("image", "")
            if image.startswith(PLACEHOLDER_PREFIX):
                pass  # substituted at deploy time; deploy script re-checks.
            elif "@sha256:" not in image:
                problems.append(Problem(path, obj, f"{where} image {image!r} is not pinned to a digest. Mutable tags make rollback unreliable."))

            csc = c.get("securityContext", {}) or {}
            if csc.get("allowPrivilegeEscalation") is not False:
                problems.append(Problem(path, obj, f"{where} must set allowPrivilegeEscalation: false."))
            if csc.get("privileged"):
                problems.append(Problem(path, obj, f"{where} must not be privileged."))
            drops = ((csc.get("capabilities") or {}).get("drop")) or []
            if "ALL" not in drops:
                problems.append(Problem(path, obj, f"{where} must drop ALL capabilities."))

            res = c.get("resources", {}) or {}
            for bucket in ("requests", "limits"):
                for resource in ("cpu", "memory"):
                    if (res.get(bucket) or {}).get(resource) is None:
                        problems.append(Problem(path, obj, f"{where} is missing resources.{bucket}.{resource}."))

        # hostPath, host networking and host namespaces are all forbidden
        # under the restricted profile and would break namespace isolation.
        for volume in spec.get("volumes") or []:
            if "hostPath" in volume:
                problems.append(Problem(path, obj, f"{descriptor} mounts hostPath {volume.get('name')!r}, which the restricted profile forbids."))
        for field in ("hostNetwork", "hostPID", "hostIPC"):
            if spec.get(field):
                problems.append(Problem(path, obj, f"{descriptor}.{field} must not be set."))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    problems: list[Problem] = []
    documents = 0
    files = 0

    for root in argv[1:]:
        for path in sorted(pathlib.Path(root).rglob("*.yaml")):
            # Dotfiles alongside the manifests are tool configuration
            # (kubernetes/.checkov.yaml), never Kubernetes objects.
            if path.name.startswith("."):
                continue
            files += 1
            try:
                docs = list(yaml.safe_load_all(path.read_text()))
            except (OSError, yaml.YAMLError) as exc:
                print(f"FATAL: could not parse {path}: {exc}", file=sys.stderr)
                return 2
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                documents += 1
                check_document(doc, str(path), problems)

    print(f"Checked {documents} manifest(s) across {files} file(s).")

    if problems:
        print(f"\nREJECTED: {len(problems)} policy violation(s)\n")
        for p in problems:
            print(p)
        return 1

    print("PASS: cost, data-protection and Pod Security invariants all hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
