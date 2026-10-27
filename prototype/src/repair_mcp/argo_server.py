"""Read-only Argo CD / Argo Workflows MCP server ("argo")."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from . import common as c

MAX_NODES = 60


def _apps(api: Any) -> list[dict]:
    resp = api.list_namespaced_custom_object("argoproj.io", "v1alpha1", c.ARGOCD_NS, "applications")
    return resp.get("items") or []


@c.tool
def argo_app_for_namespace(namespace: str, *, api: Any = None) -> Any:
    c.check_namespace(namespace)
    api = api or c.custom_api()
    for app in _apps(api):
        spec = app.get("spec") or {}
        if (spec.get("destination") or {}).get("namespace") != namespace:
            continue
        src = spec.get("source") or (spec.get("sources") or [{}])[0]
        return {
            "app": app["metadata"]["name"],
            "repoURL": src.get("repoURL"),
            "path": src.get("path"),
            "targetRevision": src.get("targetRevision"),
        }
    raise c.ToolError(f"no Argo CD application targets namespace {namespace}")


@c.tool
def argo_app_status(app: str, *, api: Any = None) -> Any:
    api = api or c.custom_api()
    obj = api.get_namespaced_custom_object("argoproj.io", "v1alpha1", c.ARGOCD_NS, "applications", app)
    dest_ns = ((obj.get("spec") or {}).get("destination") or {}).get("namespace")
    if dest_ns not in c.allowed_namespaces():
        raise c.ToolError("namespace not allowed")
    st = obj.get("status") or {}
    resources = [
        {
            "kind": r.get("kind"),
            "name": r.get("name"),
            "namespace": r.get("namespace"),
            "sync": r.get("status"),
            "health": (r.get("health") or {}).get("status"),
            "message": c.redact((r.get("health") or {}).get("message") or ""),
        }
        for r in st.get("resources") or []
    ]
    op = st.get("operationState") or {}
    return {
        "app": app,
        "sync": (st.get("sync") or {}).get("status"),
        "revision": (st.get("sync") or {}).get("revision"),
        "health": (st.get("health") or {}).get("status"),
        "operation": {"phase": op.get("phase"), "message": c.redact(op.get("message") or "")} if op else None,
        "resources": resources[:100],
    }


@c.tool
def workflow_status(namespace: str, name: str, *, api: Any = None) -> Any:
    c.check_namespace(namespace)
    api = api or c.custom_api()
    wf = api.get_namespaced_custom_object("argoproj.io", "v1alpha1", namespace, "workflows", name)
    st = wf.get("status") or {}
    nodes = [
        {
            "name": n.get("name"),
            "displayName": n.get("displayName"),
            "phase": n.get("phase"),
            "message": c.redact(n.get("message") or ""),
            "podName": n.get("id") if n.get("type") == "Pod" else "",
            "template": n.get("templateName") or "",
        }
        for n in (st.get("nodes") or {}).values()
    ]
    return {
        "name": name,
        "phase": st.get("phase"),
        "message": c.redact(st.get("message") or ""),
        "nodes": nodes[:MAX_NODES],
    }


def build_server(api: Any = None) -> MCPServer:
    srv = MCPServer("argo")

    @srv.tool(name="argo_app_for_namespace", description="Find the Argo CD Application deploying into a namespace: {app, repoURL, path, targetRevision}.")
    def argo_app_for_namespace_(namespace: str) -> Any:
        return argo_app_for_namespace(namespace, api=api)

    @srv.tool(name="argo_app_status", description="Argo CD Application sync status, revision, health and per-resource health/message.")
    def argo_app_status_(app: str) -> Any:
        return argo_app_status(app, api=api)

    @srv.tool(name="workflow_status", description="Argo Workflow phase, message and nodes [{name, displayName, phase, message, podName, template}].")
    def workflow_status_(namespace: str, name: str) -> Any:
        return workflow_status(namespace, name, api=api)

    return srv


if __name__ == "__main__":
    build_server().run()
