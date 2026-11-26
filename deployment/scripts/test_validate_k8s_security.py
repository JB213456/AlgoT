"""Tests for the Kubernetes security scan.

As with the other gates, the value is in what it REJECTS: a scanner that
cannot fail gives false assurance. The last test asserts the real manifests
still pass, so a genuine regression in them is caught here too.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import yaml

HERE = pathlib.Path(__file__).parent
REPO_ROOT = HERE.parents[1]
MODULE_PATH = HERE / "validate-k8s-security.py"

_spec = importlib.util.spec_from_file_location("validate_k8s_security", MODULE_PATH)
assert _spec and _spec.loader
vs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vs)


def findings(doc_yaml: str) -> list[str]:
    """Run the document checks over one YAML document, return the messages."""
    problems: list = []
    doc = yaml.safe_load(doc_yaml)
    name = (doc.get("metadata") or {}).get("name", "?")
    obj = f"{doc.get('kind')}/{name}"
    vs.check_rbac(doc, "test.yaml", obj, problems)
    vs.check_pod_security(doc, "test.yaml", obj, problems)
    return [p.message for p in problems]


def network_findings(*docs_yaml: str) -> list[str]:
    problems: list = []
    docs = [("test.yaml", yaml.safe_load(d)) for d in docs_yaml]
    vs.check_network_policies(docs, problems)
    return [p.message for p in problems]


# --------------------------------------------------------------------------
# RBAC
# --------------------------------------------------------------------------

ROLE = """
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {{name: r, namespace: algot-data}}
rules:
  - apiGroups: [{groups}]
    resources: [{resources}]
    verbs: [{verbs}]
"""


def test_rejects_a_wildcard_api_group():
    out = findings(ROLE.format(groups='"*"', resources="pods", verbs="get"))
    assert any("apiGroups contains '*'" in m for m in out)


def test_rejects_a_wildcard_resource():
    out = findings(ROLE.format(groups='""', resources='"*"', verbs="get"))
    assert any("resources contains '*'" in m for m in out)


def test_rejects_a_wildcard_verb():
    out = findings(ROLE.format(groups='""', resources="pods", verbs='"*"'))
    assert any("verbs contains '*'" in m for m in out)


def test_rejects_escalation_verbs():
    out = findings(ROLE.format(groups="rbac.authorization.k8s.io", resources="roles", verbs="escalate"))
    assert any("escalate" in m for m in out)


def test_rejects_reading_secrets():
    out = findings(ROLE.format(groups='""', resources="secrets", verbs="get, list"))
    assert any("on Secrets" in m for m in out)


def test_accepts_a_narrow_read_only_role():
    out = findings(ROLE.format(groups='""', resources="pods, pods/log", verbs="get, list, watch"))
    assert out == []


def test_accepts_writing_secrets_is_not_flagged_as_bulk_read():
    # vault-secrets.sh creates Secrets; only read access is the exposure.
    out = findings(ROLE.format(groups='""', resources="secrets", verbs="create, update"))
    assert out == []


def test_rejects_binding_cluster_admin():
    out = findings(
        """
        apiVersion: rbac.authorization.k8s.io/v1
        kind: RoleBinding
        metadata: {name: b, namespace: algot-data}
        roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: cluster-admin}
        subjects: [{kind: ServiceAccount, name: x, namespace: algot-data}]
        """
    )
    assert any("cluster-admin" in m for m in out)


def test_rejects_any_cluster_role_binding():
    out = findings(
        """
        apiVersion: rbac.authorization.k8s.io/v1
        kind: ClusterRoleBinding
        metadata: {name: b}
        roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: algot-observer}
        subjects: [{kind: ServiceAccount, name: x, namespace: algot-data}]
        """
    )
    assert any("every namespace" in m for m in out)


# --------------------------------------------------------------------------
# pod hardening
# --------------------------------------------------------------------------

POD = """
apiVersion: v1
kind: Pod
metadata: {{name: p, namespace: algot-data}}
spec:
  serviceAccountName: {sa}
  automountServiceAccountToken: {automount}
  containers:
    - name: app
      image: reg/app@sha256:0000000000000000000000000000000000000000000000000000000000000000
      securityContext:
        readOnlyRootFilesystem: {rorfs}
        seccompProfile: {{type: {seccomp}}}
"""


def _pod(sa="market-data-db", automount="false", rorfs="true", seccomp="RuntimeDefault"):
    return POD.format(sa=sa, automount=automount, rorfs=rorfs, seccomp=seccomp)


def test_accepts_a_hardened_pod():
    assert findings(_pod()) == []


def test_rejects_a_default_service_account_with_a_mounted_token():
    out = findings(_pod(sa="default", automount="true"))
    assert any("default ServiceAccount" in m for m in out)


def test_accepts_the_default_service_account_when_no_token_is_mounted():
    # Nothing to steal, so this is an acceptable posture.
    out = findings(_pod(sa="default", automount="false"))
    assert out == []


def test_rejects_a_writable_root_filesystem():
    out = findings(_pod(rorfs="false"))
    assert any("readOnlyRootFilesystem" in m for m in out)


def test_rejects_a_missing_seccomp_profile():
    out = findings(
        """
        apiVersion: v1
        kind: Pod
        metadata: {name: p, namespace: algot-data}
        spec:
          serviceAccountName: market-data-db
          containers:
            - name: app
              image: reg/app@sha256:0000000000000000000000000000000000000000000000000000000000000000
              securityContext: {readOnlyRootFilesystem: true}
        """
    )
    assert any("seccompProfile" in m for m in out)


def test_accepts_a_seccomp_profile_inherited_from_the_pod():
    out = findings(
        """
        apiVersion: v1
        kind: Pod
        metadata: {name: p, namespace: algot-data}
        spec:
          serviceAccountName: market-data-db
          securityContext:
            seccompProfile: {type: RuntimeDefault}
          containers:
            - name: app
              image: reg/app@sha256:0000000000000000000000000000000000000000000000000000000000000000
              securityContext: {readOnlyRootFilesystem: true}
        """
    )
    assert out == []


def test_rejects_a_host_path_mount():
    out = findings(
        """
        apiVersion: v1
        kind: Pod
        metadata: {name: p, namespace: algot-data}
        spec:
          serviceAccountName: market-data-db
          volumes: [{name: sock, hostPath: {path: /var/run/docker.sock}}]
          containers:
            - name: app
              image: reg/app@sha256:0000000000000000000000000000000000000000000000000000000000000000
              securityContext:
                readOnlyRootFilesystem: true
                seccompProfile: {type: RuntimeDefault}
        """
    )
    assert any("hostPath" in m for m in out)


def test_rejects_a_literal_credential_in_env():
    out = findings(
        """
        apiVersion: v1
        kind: Pod
        metadata: {name: p, namespace: algot-data}
        spec:
          serviceAccountName: market-data-db
          containers:
            - name: app
              image: reg/app@sha256:0000000000000000000000000000000000000000000000000000000000000000
              env:
                - {name: ALPACA_SECRET_KEY, value: hunter2}
              securityContext:
                readOnlyRootFilesystem: true
                seccompProfile: {type: RuntimeDefault}
        """
    )
    assert any("literal value" in m for m in out)


def test_accepts_a_credential_taken_from_a_secret_reference():
    out = findings(
        """
        apiVersion: v1
        kind: Pod
        metadata: {name: p, namespace: algot-data}
        spec:
          serviceAccountName: market-data-db
          containers:
            - name: app
              image: reg/app@sha256:0000000000000000000000000000000000000000000000000000000000000000
              env:
                - name: ALPACA_SECRET_KEY
                  valueFrom:
                    secretKeyRef: {name: alpaca, key: secret}
              securityContext:
                readOnlyRootFilesystem: true
                seccompProfile: {type: RuntimeDefault}
        """
    )
    assert out == []


def test_checks_reach_inside_a_cronjob_template():
    out = findings(
        """
        apiVersion: batch/v1
        kind: CronJob
        metadata: {name: c, namespace: algot-data}
        spec:
          schedule: "0 1 * * *"
          jobTemplate:
            spec:
              template:
                spec:
                  serviceAccountName: algot-maintenance
                  containers:
                    - name: app
                      image: reg/app@sha256:0000000000000000000000000000000000000000000000000000000000000000
                      securityContext: {readOnlyRootFilesystem: false}
        """
    )
    assert any("readOnlyRootFilesystem" in m for m in out)
    assert any("seccompProfile" in m for m in out)


# --------------------------------------------------------------------------
# network policy
# --------------------------------------------------------------------------

DEFAULT_DENY = """
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: default-deny-all, namespace: algot-data}
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
"""


def test_accepts_a_default_deny_policy():
    assert network_findings(DEFAULT_DENY) == []


def test_rejects_having_no_network_policy_at_all():
    out = network_findings(
        "{apiVersion: v1, kind: ConfigMap, metadata: {name: c, namespace: algot-data}}"
    )
    assert any("no NetworkPolicy at all" in m for m in out)


def test_rejects_policies_without_a_default_deny():
    only_ingress = """
    apiVersion: networking.k8s.io/v1
    kind: NetworkPolicy
    metadata: {name: db-ingress, namespace: algot-data}
    spec:
      podSelector: {matchLabels: {app: db}}
      policyTypes: [Ingress]
      ingress: [{from: [{podSelector: {matchLabels: {app: ing}}}]}]
    """
    out = network_findings(only_ingress)
    assert any("no default-deny policy" in m for m in out)


def test_rejects_a_one_directional_default_policy():
    # Egress-only "deny" leaves ingress wide open.
    out = network_findings(
        """
        apiVersion: networking.k8s.io/v1
        kind: NetworkPolicy
        metadata: {name: half, namespace: algot-data}
        spec:
          podSelector: {}
          policyTypes: [Egress]
        """
    )
    assert any("no default-deny policy" in m for m in out)


def test_rejects_a_namespace_wide_allow_all():
    out = network_findings(
        DEFAULT_DENY,
        """
        apiVersion: networking.k8s.io/v1
        kind: NetworkPolicy
        metadata: {name: open-everything, namespace: algot-data}
        spec:
          podSelector: {}
          policyTypes: [Ingress]
          ingress: [{}]
        """,
    )
    assert any("cancels the default deny" in m for m in out)


# --------------------------------------------------------------------------
# secret literals and end to end
# --------------------------------------------------------------------------


def test_rejects_an_inline_private_key():
    problems: list = []
    vs.check_secret_literals(
        "data:\n  key: |\n    -----BEGIN RSA PRIVATE KEY-----\n", "t.yaml", problems
    )
    assert any("private key" in p.message for p in problems)


def test_rejects_a_literal_ocid():
    problems: list = []
    vs.check_secret_literals(
        "  volumeHandle: ocid1.volume.oc1.uk-london-1.abcdefghijklmnopqrstuvwxyz012345\n",
        "t.yaml",
        problems,
    )
    assert any("OCID" in p.message for p in problems)


def test_ignores_a_commented_example():
    problems: list = []
    vs.check_secret_literals(
        "# e.g. ocid1.volume.oc1.uk-london-1.abcdefghijklmnopqrstuvwxyz012345\n",
        "t.yaml",
        problems,
    )
    assert problems == []


def test_the_real_manifests_pass():
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), str(REPO_ROOT / "kubernetes")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout
