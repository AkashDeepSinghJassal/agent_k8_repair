#!/usr/bin/env bash
# Idempotent bootstrap of the local kind cluster "repair" (CONTRACTS §3):
# kind cluster, Argo CD, Argo Workflows, Argo CD Applications, WorkflowTemplate.
# Safe to re-run.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CLUSTER="repair"
CONTEXT="kind-${CLUSTER}"
ARGOCD_CHART_VERSION="10.10.5"      # argo/argo-cd, app v3.5.5
ARGO_WF_CHART_VERSION="2.0.14"      # argo/argo-workflows, app v4.1.6
WF_TEMPLATE_URL="${WF_TEMPLATE_URL:-https://raw.githubusercontent.com/AkashDeepSinghJassal/repair-demo-app/main/workflows/release-pipeline.yaml}"

log() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }

for tool in kind kubectl helm; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 1; }
done

log "kind cluster '${CLUSTER}'"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "cluster already exists, skipping create"
else
  kind create cluster --name "$CLUSTER" --config "$HERE/kind-config.yaml"
fi
kubectl config use-context "$CONTEXT" >/dev/null

log "Helm repo argo"
helm repo add argo https://argoproj.github.io/argo-helm --force-update >/dev/null
helm repo update argo >/dev/null

# Argo Workflows' chart creates RBAC in each controller.workflowNamespaces entry,
# so those namespaces must exist first. Argo CD (CreateNamespace=true) also tolerates them.
log "Namespaces"
for ns in argocd argo demo demo-b; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done

log "Argo CD ${ARGOCD_CHART_VERSION}"
helm upgrade --install argo-cd argo/argo-cd --version "$ARGOCD_CHART_VERSION" \
  --namespace argocd -f "$HERE/values-argocd.yaml" --wait --timeout 10m
kubectl -n argocd rollout status deployment/argo-cd-argocd-server --timeout=5m

log "Argo Workflows ${ARGO_WF_CHART_VERSION}"
helm upgrade --install argo-workflows argo/argo-workflows --version "$ARGO_WF_CHART_VERSION" \
  --namespace argo -f "$HERE/values-argo-workflows.yaml" --wait --timeout 10m
kubectl -n argo rollout status deployment/argo-workflows-server --timeout=5m

log "Argo CD Applications"
kubectl apply -f "$HERE/apps.yaml"

log "WorkflowTemplate from repair-demo-app"
if kubectl apply -f "$WF_TEMPLATE_URL"; then
  echo "applied $WF_TEMPLATE_URL"
else
  warn "could not apply $WF_TEMPLATE_URL (is repair-demo-app created yet?); re-run bootstrap.sh once it exists"
fi

log "Access"
echo "Argo CD admin password:"
echo "  kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo"
echo "Argo CD UI (user admin, https):   kubectl -n argocd port-forward svc/argocd-server 8443:443"
echo "Argo Workflows UI (http):         kubectl -n argo port-forward svc/argo-workflows-server 2746:2746"
echo "Demo frontend:                    http://localhost:8080"
