#!/usr/bin/env bash
# Cleanup for `quickstart.sh`. Stops the port-forward, uninstalls the helm
# release, drops the namespace, deletes the kind cluster, and clears
# .metis-trial state. Idempotent — safe to re-run.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

STATE_DIR="${METIS_TRIAL_STATE_DIR:-$REPO_ROOT/.metis-trial}"
STATE_FILE="$STATE_DIR/state.env"

# Defaults if state file is missing (e.g. user blew it away mid-cycle).
CLUSTER_NAME="${METIS_TRIAL_CLUSTER:-metis-trial}"
NAMESPACE="${METIS_TRIAL_NAMESPACE:-metis-gateway}"
RELEASE="${METIS_TRIAL_RELEASE:-metis-gateway}"

if [[ -f "$STATE_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$STATE_FILE"
fi

echo "==> Stopping port-forward (if any)"
if [[ -f "$STATE_DIR/port-forward.pid" ]]; then
  kill "$(cat "$STATE_DIR/port-forward.pid")" 2>/dev/null || true
  rm -f "$STATE_DIR/port-forward.pid"
fi

if command -v helm >/dev/null 2>&1 && command -v kubectl >/dev/null 2>&1; then
  if kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    echo "==> helm uninstall $RELEASE"
    helm uninstall "$RELEASE" --namespace "$NAMESPACE" >/dev/null 2>&1 || true
    echo "==> Deleting namespace $NAMESPACE"
    kubectl delete namespace "$NAMESPACE" --wait=true >/dev/null 2>&1 || true
  fi
fi

if command -v kind >/dev/null 2>&1; then
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
    echo "==> Deleting kind cluster $CLUSTER_NAME"
    kind delete cluster --name "$CLUSTER_NAME" >/dev/null
  fi
fi

echo "==> Removing $STATE_DIR"
rm -rf "$STATE_DIR"

echo "Done."
