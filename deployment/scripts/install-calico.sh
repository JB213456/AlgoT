#!/usr/bin/env bash
# Install Calico in policy-only mode for NetworkPolicy enforcement.
#
# Why this is necessary: OKE's NetworkPolicy objects are inert without a
# policy engine. Oracle does not support Calico alongside the flannel CNI
# ("causes network issues"), so this cluster uses VCN-Native Pod Networking,
# which is the configuration where Oracle supports Calico policy-only.
#
# Oracle's OKE instructions require removing the upstream CNI installer and
# host CNI mounts, plus setting Felix options for OCI interfaces. The
# upstream policy-only file alone still installs 10-calico.conflist and
# overrides the OCI CNI.
#
# Calico version must be one Oracle has tested against the cluster's
# Kubernetes version. v3.30.3 is the tested pairing for Kubernetes 1.34.
set -euo pipefail

CALICO_VERSION="${CALICO_VERSION:-v3.30.3}"
MANIFEST_URL="https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/calico-policy-only.yaml"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "==> Confirming the cluster uses VCN-native pod networking"
# Calico policy-only must not be installed on a flannel cluster.
if kubectl -n kube-system get daemonset kube-flannel-ds >/dev/null 2>&1; then
  cat >&2 <<'MSG'
REFUSING: this cluster still runs the flannel CNI (kube-flannel-ds exists).

Oracle does not support Calico alongside flannel; installing it causes
network issues. The cluster must use VCN-Native Pod Networking instead.
Set cni_type = "OCI_VCN_IP_NATIVE" in the Terraform kubernetes module and
re-apply (this replaces the cluster), then re-run this script.
MSG
  exit 1
fi

if ! kubectl -n kube-system get daemonset vcn-native-ip-cni >/dev/null 2>&1; then
  echo "WARNING: could not find the vcn-native-ip-cni daemonset." >&2
  echo "         Verify the cluster's CNI before continuing." >&2
fi
echo "    VCN-native pod networking confirmed"

echo "==> Fetching Calico ${CALICO_VERSION} policy-only manifest"
if [[ -n "${CALICO_MANIFEST:-}" ]]; then
  cp "$CALICO_MANIFEST" "$WORK_DIR/calico.yaml"
elif ! curl -fsSL "$MANIFEST_URL" -o "$WORK_DIR/calico.yaml"; then
  # Some operator networks resolve api.github.com but not the raw host.
  gh api "repos/projectcalico/calico/contents/manifests/calico-policy-only.yaml?ref=${CALICO_VERSION}" \
    --jq .content | base64 -d > "$WORK_DIR/calico.yaml"
fi

# Sanity-check that this really is the policy-only variant before applying
# something that could take over pod networking.
for required in 'CALICO_NETWORKING_BACKEND' 'CALICO_MANAGE_CNI'; do
  grep -q "$required" "$WORK_DIR/calico.yaml" || {
    echo "FATAL: manifest is missing $required; this may not be the policy-only variant." >&2
    exit 1
  }
done
if ! grep -A1 'CALICO_MANAGE_CNI' "$WORK_DIR/calico.yaml" | grep -q '"false"'; then
  echo "FATAL: CALICO_MANAGE_CNI is not false. Refusing to let Calico manage the CNI." >&2
  exit 1
fi
echo "    verified: policy-only (Calico will not manage the CNI)"

# Apply Oracle's VCN-native adaptations to the Calico DaemonSet only.
echo "==> Adapting Calico policy-only to OKE VCN-native CNI"
python3 - "$WORK_DIR/calico.yaml" <<'PY'
import sys, pathlib, yaml

path = pathlib.Path(sys.argv[1])
docs = list(yaml.safe_load_all(path.read_text()))
daemonsets = [d for d in docs if isinstance(d, dict) and d.get("kind") == "DaemonSet"
              and d.get("metadata", {}).get("name") == "calico-node"]
if len(daemonsets) != 1:
    sys.exit("FATAL: expected one calico-node DaemonSet")
spec = daemonsets[0]["spec"]["template"]["spec"]
spec.pop("initContainers", None)
spec["volumes"] = [v for v in spec["volumes"]
                   if v["name"] not in {"cni-bin-dir", "cni-net-dir", "cni-log-dir"}]
node = next(c for c in spec["containers"] if c["name"] == "calico-node")
node.pop("envFrom", None)
node["env"] = [e for e in node["env"] if e["name"] != "FELIX_TYPHAK8SSERVICENAME"]
node["volumeMounts"] = [m for m in node["volumeMounts"]
                        if m["name"] not in {"cni-net-dir", "cni-log-dir"}]
for name, value in {
    "FELIX_INTERFACEPREFIX": "oci",
    "NO_DEFAULT_POOLS": "true",
    "FELIX_CHAININSERTMODE": "Append",
    "FELIX_IPTABLESMANGLEALLOWACTION": "Return",
    "FELIX_IPTABLESBACKEND": "NFT",
}.items():
    node["env"] = [e for e in node["env"] if e["name"] != name]
    node["env"].append({"name": name, "value": value})
path.write_text(yaml.safe_dump_all(docs, explicit_start=True, sort_keys=False))
print("    CNI installer removed; Felix configured for OCI interfaces")
PY

# A previous unadapted installation may have left Calico's CNI file ahead
# of 10-oci.conflist. Remove only those two Calico-created files while the
# existing daemonset still has its host mount. Never touch the OCI file.
for pod in $(kubectl -n kube-system get pods -l k8s-app=calico-node -o name 2>/dev/null); do
  kubectl -n kube-system exec "$pod" -c calico-node -- \
    rm -f /host/etc/cni/net.d/10-calico.conflist /host/etc/cni/net.d/calico-kubeconfig
done

echo "==> Applying Calico"
kubectl apply --server-side --force-conflicts -f "$WORK_DIR/calico.yaml"

echo "==> Waiting for Calico to become ready"
kubectl -n kube-system rollout status daemonset/calico-node --timeout=300s
kubectl -n kube-system rollout status deployment/calico-kube-controllers --timeout=300s

echo
kubectl -n kube-system get pods -l k8s-app=calico-node -o wide
echo
echo "Calico ${CALICO_VERSION} installed in policy-only mode."
echo "Prove enforcement with: deployment/scripts/verify-network-policies.sh"
