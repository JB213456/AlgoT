"""The Kubernetes manifests must keep satisfying the cost and security
invariants, and the validator that checks them must actually reject breaches.
"""
from __future__ import annotations

import importlib.util
import pathlib
import textwrap

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
VALIDATOR = REPO_ROOT / "deployment" / "scripts" / "validate-k8s-manifests.py"
MANIFEST_DIR = REPO_ROOT / "kubernetes"


def load_validator():
    spec = importlib.util.spec_from_file_location("validate_k8s_manifests", VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def validator():
    pytest.importorskip("yaml")
    return load_validator()


def problems_for(validator, yaml_text: str):
    import yaml

    found = []
    for doc in yaml.safe_load_all(textwrap.dedent(yaml_text)):
        if isinstance(doc, dict):
            validator.check_document(doc, "test.yaml", found)
    return [p.message for p in found]


def test_repository_manifests_satisfy_policy(validator):
    assert validator.main([str(VALIDATOR), str(MANIFEST_DIR)]) == 0


def test_rejects_loadbalancer_service(validator):
    messages = problems_for(validator, """
        apiVersion: v1
        kind: Service
        metadata: {name: db}
        spec:
          type: LoadBalancer
    """)
    assert any("billable OCI load balancer" in m for m in messages)


def test_rejects_ingress(validator):
    messages = problems_for(validator, """
        apiVersion: networking.k8s.io/v1
        kind: Ingress
        metadata: {name: exposed}
    """)
    assert any("billable load balancer" in m for m in messages)


def test_rejects_mutable_image_tag(validator):
    messages = problems_for(validator, """
        apiVersion: apps/v1
        kind: Deployment
        metadata: {name: app}
        spec:
          template:
            spec:
              securityContext: {runAsNonRoot: true}
              containers:
                - name: c
                  image: ghcr.io/owner/algot-database:latest
                  securityContext:
                    allowPrivilegeEscalation: false
                    capabilities: {drop: [ALL]}
                  resources:
                    requests: {cpu: 10m, memory: 32Mi}
                    limits: {cpu: 100m, memory: 64Mi}
    """)
    assert any("not pinned to a digest" in m for m in messages)


def test_rejects_non_retaining_persistent_volume(validator):
    messages = problems_for(validator, """
        apiVersion: v1
        kind: PersistentVolume
        metadata: {name: pv}
        spec:
          persistentVolumeReclaimPolicy: Delete
    """)
    assert any("must be Retain" in m for m in messages)


def test_rejects_quota_permitting_load_balancers(validator):
    messages = problems_for(validator, """
        apiVersion: v1
        kind: ResourceQuota
        metadata: {name: q}
        spec:
          hard:
            services.loadbalancers: "1"
            services.nodeports: "0"
    """)
    assert any("services.loadbalancers" in m for m in messages)


def test_rejects_namespace_without_restricted_pod_security(validator):
    messages = problems_for(validator, """
        apiVersion: v1
        kind: Namespace
        metadata:
          name: algot-data
          labels:
            pod-security.kubernetes.io/enforce: baseline
    """)
    assert any("enforce=restricted" in m for m in messages)


def test_rejects_hostpath_and_host_namespaces(validator):
    messages = problems_for(validator, """
        apiVersion: apps/v1
        kind: Deployment
        metadata: {name: app}
        spec:
          template:
            spec:
              hostNetwork: true
              securityContext: {runAsNonRoot: true}
              containers:
                - name: c
                  image: ghcr.io/o/x@sha256:0000000000000000000000000000000000000000000000000000000000000000
                  securityContext:
                    allowPrivilegeEscalation: false
                    capabilities: {drop: [ALL]}
                  resources:
                    requests: {cpu: 10m, memory: 32Mi}
                    limits: {cpu: 100m, memory: 64Mi}
              volumes:
                - name: root
                  hostPath: {path: /}
    """)
    assert any("hostPath" in m for m in messages)
    assert any("hostNetwork" in m for m in messages)


def test_cronjob_pod_templates_are_inspected(validator):
    # A CronJob nests its pod spec two levels deeper; a validator that missed
    # this would silently skip the backup and monitoring jobs.
    messages = problems_for(validator, """
        apiVersion: batch/v1
        kind: CronJob
        metadata: {name: job}
        spec:
          schedule: "0 2 * * 0"
          jobTemplate:
            spec:
              template:
                spec:
                  containers:
                    - name: c
                      image: ghcr.io/o/x:mutable
    """)
    assert any("not pinned to a digest" in m for m in messages)
    assert any("runAsNonRoot" in m for m in messages)


# --- namespace scope: only algot-data may be deployed into ---------------


def test_rejects_an_object_with_no_namespace(validator):
    # Without metadata.namespace the object lands in whichever namespace the
    # caller's kubectl context happens to select, which is how a workload
    # ends up outside algot-data without anyone choosing that.
    messages = problems_for(validator, """
        apiVersion: v1
        kind: ConfigMap
        metadata: {name: scripts}
    """)
    assert any("no metadata.namespace" in m for m in messages)


def test_rejects_an_object_in_another_namespace(validator):
    messages = problems_for(validator, """
        apiVersion: v1
        kind: ConfigMap
        metadata: {name: scripts, namespace: kube-system}
    """)
    assert any("only algot-data may be deployed into" in m for m in messages)


def test_rejects_creating_a_different_namespace(validator):
    messages = problems_for(validator, """
        apiVersion: v1
        kind: Namespace
        metadata:
          name: algot-trading
          labels: {pod-security.kubernetes.io/enforce: restricted}
    """)
    assert any("may only create the algot-data namespace" in m for m in messages)


def test_accepts_the_target_namespace(validator):
    messages = problems_for(validator, """
        apiVersion: v1
        kind: ConfigMap
        metadata: {name: scripts, namespace: algot-data}
    """)
    assert messages == []


def test_cluster_scoped_kinds_need_no_namespace(validator):
    # A StorageClass or PersistentVolume has no namespace by definition;
    # demanding one would be wrong.
    messages = problems_for(validator, """
        apiVersion: storage.k8s.io/v1
        kind: StorageClass
        metadata: {name: oci-bv-retain}
        provisioner: blockvolume.csi.oraclecloud.com
        reclaimPolicy: Retain
    """)
    assert messages == []


def test_rejects_a_namespace_field_on_a_cluster_scoped_kind(validator):
    messages = problems_for(validator, """
        apiVersion: storage.k8s.io/v1
        kind: StorageClass
        metadata: {name: oci-bv-retain, namespace: algot-data}
        provisioner: blockvolume.csi.oraclecloud.com
        reclaimPolicy: Retain
    """)
    assert any("cluster-scoped" in m for m in messages)
