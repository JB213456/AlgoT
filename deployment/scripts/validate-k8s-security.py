#!/usr/bin/env python3
"""Security scan of the AlgoT Kubernetes manifests.

This is the security half of the manifest gates. validate-k8s-manifests.py
asserts the cost, data-protection and Pod Security invariants; this asserts
the ones an attacker would exploit:

  rbac           No wildcard apiGroups, resources or verbs; no privilege
                 escalation verbs; no cluster-admin binding; no blanket
                 read over Secrets. A Role granting `*` is indistinguishable
                 from cluster-admin inside the namespace.

  tokens         Every pod either runs as a purpose-built ServiceAccount or
                 refuses the token mount. A mounted default-SA token is a
                 free credential for anything that achieves code execution.

  network        A default-deny NetworkPolicy covering both directions
                 exists, and nothing re-opens the whole namespace. The
                 database holds the market data and must not be reachable
                 or able to reach out except where a policy says so.

  hardening      seccompProfile RuntimeDefault, read-only root filesystem,
                 and no mounted Docker socket or host path.

  secrets        No credential literal in a manifest. Values come from OCI
                 Vault via vault-secrets.sh, never from a committed file.

These run on untrusted pull requests, so this script reads manifests only:
no cluster, no credentials, no network.

Usage:  validate-k8s-security.py <dir> [<dir> ...]
Exit:   0 clean, 1 violations found, 2 could not read manifests
"""

from __future__ import annotations

import pathlib
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - environment problem
    sys.exit("FATAL: PyYAML is required (pip install pyyaml)")

ROLE_KINDS = {"Role", "ClusterRole"}
BINDING_KINDS = {"RoleBinding", "ClusterRoleBinding"}

# Verbs that let a subject grant itself more than it was given.
ESCALATION_VERBS = {"escalate", "bind", "impersonate"}

# Roles that must never be bound by this repository's manifests.
FORBIDDEN_ROLES = {"cluster-admin", "edit", "admin"}

WORKLOAD_KINDS = {
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "Job",
    "CronJob",
    "Pod",
    "ReplicaSet",
}

# A value that looks like a real credential rather than a reference. The
# manifests legitimately contain secretKeyRef names, which are not values.
SECRET_VALUE_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "an inline private key"),
    (re.compile(r"\bocid1\.[a-z]+\.oc1\.[a-z0-9-]*\.[a-z0-9]{20,}"), "a literal OCID"),
    (re.compile(r"\b[A-Za-z0-9/+]{40}\b.*(?:secret|token|password)", re.I), "a literal credential"),
]

# Environment variable names whose value must come from a secret reference.
CREDENTIAL_ENV = re.compile(
    r"(PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|APIKEY|PRIVATE_KEY|CREDENTIAL)", re.I
)


class Problem:
    def __init__(self, path: str, obj: str, message: str) -> None:
        self.path, self.obj, self.message = path, obj, message

    def __str__(self) -> str:
        return f"  {self.path} [{self.obj}]\n      {self.message}"


def pod_specs(doc: dict):
    """Yield (descriptor, podSpec) for every pod template in a document."""
    kind = doc.get("kind")
    spec = doc.get("spec") or {}
    if kind == "Pod":
        yield "spec", spec
    elif kind == "CronJob":
        job = ((spec.get("jobTemplate") or {}).get("spec") or {})
        template = (job.get("template") or {}).get("spec")
        if template:
            yield "spec.jobTemplate.spec.template.spec", template
    elif kind in WORKLOAD_KINDS:
        template = (spec.get("template") or {}).get("spec")
        if template:
            yield "spec.template.spec", template


def all_containers(pod_spec: dict):
    for section in ("initContainers", "containers"):
        for c in pod_spec.get(section) or []:
            yield section, c


def check_rbac(doc: dict, path: str, obj: str, problems: list[Problem]) -> None:
    kind = doc.get("kind")

    if kind in ROLE_KINDS:
        for index, rule in enumerate(doc.get("rules") or []):
            where = f"rules[{index}]"
            groups = [str(g) for g in (rule.get("apiGroups") or [])]
            resources = [str(r) for r in (rule.get("resources") or [])]
            verbs = [str(v) for v in (rule.get("verbs") or [])]

            for field, values in (
                ("apiGroups", groups),
                ("resources", resources),
                ("verbs", verbs),
            ):
                if "*" in values:
                    problems.append(
                        Problem(
                            path,
                            obj,
                            f"{where}.{field} contains '*'. A wildcard rule grants "
                            "everything the API server will ever add, including "
                            "resources that do not exist yet. Name the resources.",
                        )
                    )

            escalations = sorted(ESCALATION_VERBS.intersection(v.lower() for v in verbs))
            if escalations:
                problems.append(
                    Problem(
                        path,
                        obj,
                        f"{where}.verbs grants {', '.join(escalations)}, which lets the "
                        "subject award itself permissions beyond this Role.",
                    )
                )

            # Reading Secrets in bulk defeats the point of keeping the
            # credentials in OCI Vault: whatever can list them has them all.
            if "secrets" in [r.lower() for r in resources]:
                reads = {"get", "list", "watch"}.intersection(v.lower() for v in verbs)
                if reads:
                    problems.append(
                        Problem(
                            path,
                            obj,
                            f"{where} grants {', '.join(sorted(reads))} on Secrets. "
                            "Workloads receive their credentials through a mounted "
                            "secret reference; nothing in this namespace needs to read "
                            "Secrets through the API.",
                        )
                    )

    if kind in BINDING_KINDS:
        role_ref = doc.get("roleRef") or {}
        role_name = str(role_ref.get("name", ""))
        if role_name in FORBIDDEN_ROLES:
            problems.append(
                Problem(
                    path,
                    obj,
                    f"binds the built-in {role_name!r} ClusterRole. Bind a "
                    "purpose-built Role that names only the resources needed.",
                )
            )
        if kind == "ClusterRoleBinding":
            problems.append(
                Problem(
                    path,
                    obj,
                    "a ClusterRoleBinding grants permission across every namespace. "
                    "This repository deploys only into algot-data; use a RoleBinding.",
                )
            )


def check_pod_security(
    doc: dict, path: str, obj: str, problems: list[Problem]
) -> None:
    for descriptor, spec in pod_specs(doc):
        # --- token exposure ---
        sa_name = spec.get("serviceAccountName") or spec.get("serviceAccount")
        automount = spec.get("automountServiceAccountToken")
        if not sa_name or sa_name == "default":
            # Running as the default ServiceAccount is acceptable only if no
            # token is mounted, which leaves nothing to steal.
            if automount is not False:
                problems.append(
                    Problem(
                        path,
                        obj,
                        f"{descriptor} runs as the default ServiceAccount with a "
                        "mounted token. Set a purpose-built serviceAccountName, or "
                        "automountServiceAccountToken: false.",
                    )
                )

        # --- hardening ---
        pod_sc = spec.get("securityContext") or {}
        pod_seccomp = (pod_sc.get("seccompProfile") or {}).get("type")

        for volume in spec.get("volumes") or []:
            if "hostPath" in volume:
                hp = (volume.get("hostPath") or {}).get("path", "")
                problems.append(
                    Problem(
                        path,
                        obj,
                        f"{descriptor} mounts hostPath {hp!r}, which reaches outside "
                        "the container and outside the namespace boundary.",
                    )
                )

        for section, container in all_containers(spec):
            cname = container.get("name", "?")
            where = f"{descriptor}.{section}[{cname}]"
            csc = container.get("securityContext") or {}

            seccomp = (csc.get("seccompProfile") or {}).get("type") or pod_seccomp
            if seccomp not in ("RuntimeDefault", "Localhost"):
                problems.append(
                    Problem(
                        path,
                        obj,
                        f"{where} has seccompProfile {seccomp!r}; the restricted Pod "
                        "Security Standard requires RuntimeDefault, which blocks the "
                        "syscalls a container workload never needs.",
                    )
                )

            if csc.get("readOnlyRootFilesystem") is not True:
                problems.append(
                    Problem(
                        path,
                        obj,
                        f"{where} must set readOnlyRootFilesystem: true, so a "
                        "compromised process cannot persist a payload in the image "
                        "layer. Mount an emptyDir for paths that need writing.",
                    )
                )

            # --- credentials must be references, not values ---
            for env in container.get("env") or []:
                name = str(env.get("name", ""))
                if "value" in env and CREDENTIAL_ENV.search(name):
                    problems.append(
                        Problem(
                            path,
                            obj,
                            f"{where} sets {name} to a literal value. Credentials come "
                            "from OCI Vault through a secretKeyRef, never from a "
                            "committed manifest.",
                        )
                    )


def check_network_policies(docs: list[tuple[str, dict]], problems: list[Problem]) -> None:
    """A default-deny policy must exist and cover both directions."""
    policies = [
        (path, doc)
        for path, doc in docs
        if doc.get("kind") == "NetworkPolicy"
    ]
    if not policies:
        problems.append(
            Problem(
                "kubernetes/networking",
                "NetworkPolicy",
                "no NetworkPolicy at all: every pod would be reachable from every "
                "namespace in the cluster.",
            )
        )
        return

    def is_default_deny(doc: dict) -> bool:
        spec = doc.get("spec") or {}
        selector = spec.get("podSelector")
        # An empty selector is every pod in the namespace.
        if selector not in ({}, None):
            return False
        types = {str(t) for t in (spec.get("policyTypes") or [])}
        if not {"Ingress", "Egress"}.issubset(types):
            return False
        # Deny means no allow rules at all.
        return not spec.get("ingress") and not spec.get("egress")

    if not any(is_default_deny(doc) for _, doc in policies):
        problems.append(
            Problem(
                "kubernetes/networking",
                "NetworkPolicy",
                "no default-deny policy (empty podSelector, policyTypes [Ingress, "
                "Egress], no rules). Without one, a pod with no matching policy is "
                "fully open in both directions.",
            )
        )

    # A namespace-wide allow-all would silently undo the default deny.
    for path, doc in policies:
        spec = doc.get("spec") or {}
        name = (doc.get("metadata") or {}).get("name", "?")
        if spec.get("podSelector") not in ({}, None):
            continue
        for direction in ("ingress", "egress"):
            for rule in spec.get(direction) or []:
                # `- {}` or a rule with no `from`/`to` means from/to anywhere.
                peers = rule.get("from" if direction == "ingress" else "to")
                if not peers and not rule.get("ports"):
                    problems.append(
                        Problem(
                            path,
                            f"NetworkPolicy/{name}",
                            f"an unrestricted {direction} rule applies to every pod in "
                            "the namespace, which cancels the default deny.",
                        )
                    )


def check_secret_literals(text: str, path: str, problems: list[Problem]) -> None:
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pattern, why in SECRET_VALUE_PATTERNS:
            if pattern.search(line):
                problems.append(
                    Problem(
                        path,
                        f"line {line_no}",
                        f"contains what looks like {why}. Infrastructure identifiers "
                        "are substituted from Terraform outputs at deploy time and "
                        "credentials come from OCI Vault; neither belongs in a "
                        "committed manifest.",
                    )
                )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    files: list[pathlib.Path] = []
    for target in argv[1:]:
        root = pathlib.Path(target)
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            # Dotfiles alongside the manifests are tool configuration
            # (kubernetes/.checkov.yaml), never Kubernetes objects.
            for pattern in ("*.yaml", "*.yml"):
                files += sorted(
                    p
                    for p in root.rglob(pattern)
                    if p.is_file() and not p.name.startswith(".")
                )
        else:
            print(f"FATAL: {target} is neither a file nor a directory", file=sys.stderr)
            return 2

    if not files:
        print("FATAL: no manifests found", file=sys.stderr)
        return 2

    problems: list[Problem] = []
    docs: list[tuple[str, dict]] = []

    for file in sorted(set(files)):
        path = str(file)
        text = file.read_text()
        check_secret_literals(text, path, problems)
        try:
            loaded = list(yaml.safe_load_all(text))
        except yaml.YAMLError as exc:
            print(f"FATAL: cannot parse {path}: {exc}", file=sys.stderr)
            return 2
        for doc in loaded:
            if isinstance(doc, dict) and doc:
                docs.append((path, doc))

    for path, doc in docs:
        kind = doc.get("kind", "?")
        name = (doc.get("metadata") or {}).get("name", "?")
        obj = f"{kind}/{name}"
        check_rbac(doc, path, obj, problems)
        check_pod_security(doc, path, obj, problems)

    check_network_policies(docs, problems)

    print(f"Scanned {len(docs)} manifest(s) across {len(set(files))} file(s).")

    if problems:
        print(f"\nREJECTED: {len(problems)} security finding(s)\n", file=sys.stderr)
        for problem in problems:
            print(problem, file=sys.stderr)
        print(file=sys.stderr)
        return 1

    print("PASS: RBAC, token, network, hardening and secret-literal checks all hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
