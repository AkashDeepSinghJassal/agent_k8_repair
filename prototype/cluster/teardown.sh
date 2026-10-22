#!/usr/bin/env bash
# Delete the local kind cluster "repair". Idempotent: does nothing if it is absent.
set -euo pipefail

CLUSTER="repair"

command -v kind >/dev/null || { echo "missing required tool: kind" >&2; exit 1; }

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  kind delete cluster --name "$CLUSTER"
else
  echo "cluster '${CLUSTER}' does not exist, nothing to do"
fi
