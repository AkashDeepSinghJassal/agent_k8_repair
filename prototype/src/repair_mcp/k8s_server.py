"""Read-only Kubernetes MCP server ("k8s")."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from . import common as c

KINDS = {
    "pods": ("core", "list_namespaced_pod", "read_namespaced_pod"),
    "deployments": ("apps", "list_namespaced_deployment", "read_namespaced_deployment"),
    "replicasets": ("apps", "list_namespaced_replica_set", "read_namespaced_replica_set"),
    "services": ("core", "list_namespaced_service", "read_namespaced_service"),
    "endpoints": ("core", "list_namespaced_endpoints", "read_namespaced_endpoints"),
    "configmaps": ("core", "list_namespaced_config_map", "read_namespaced_config_map"),
    "networkpolicies": ("net", "list_namespaced_network_policy", "read_namespaced_network_policy"),
    "events": ("core", "list_namespaced_event", "read_namespaced_event"),
    "persistentvolumeclaims": (
        "core", "list_namespaced_persistent_volume_claim", "read_namespaced_persistent_volume_claim",
    ),
    "secrets": ("core", "list_namespaced_secret", "read_namespaced_secret"),
}
MAX_EVENTS = 50


class Apis:
    """Lazy bundle of API clients; tests pass fakes."""

    def __init__(self, core: Any = None, apps: Any = None, net: Any = None):
        self._c, self._a, self._n = core, apps, net

    @property
    def core(self) -> Any:
        self._c = self._c or c.core_api()
        return self._c

    @property
    def apps(self) -> Any:
        self._a = self._a or c.apps_api()
        return self._a

    @property
    def net(self) -> Any:
        self._n = self._n or c.net_api()
        return self._n

    def get(self, group: str) -> Any:
        return getattr(self, group)


def _kind(kind: str) -> tuple[str, str, str]:
    key = kind.lower()
    if key not in KINDS:
        raise c.ToolError(f"unknown kind {kind!r}; use one of {sorted(KINDS)}")
    return KINDS[key]


def _items(resp: Any) -> list[dict]:
    raw = resp.get("items") if isinstance(resp, dict) else getattr(resp, "items", None)
    return [c.plain(i) for i in raw or []]


def _summary(kind: str, o: dict) -> str:
    st, spec = o.get("status") or {}, o.get("spec") or {}
    if kind == "pods":
        cs = st.get("containerStatuses") or []
        ready = sum(1 for x in cs if x.get("ready"))
        waiting = [
            (x.get("state") or {}).get("waiting", {}).get("reason") for x in cs if (x.get("state") or {}).get("waiting")
        ]
        restarts = sum(x.get("restartCount", 0) for x in cs)
        return f"{st.get('phase')} ready={ready}/{len(cs)} restarts={restarts}" + (f" waiting={waiting}" if waiting else "")
    if kind in ("deployments", "replicasets"):
        return f"replicas={spec.get('replicas')} ready={st.get('readyReplicas', 0)} available={st.get('availableReplicas', 0)}"
    if kind == "services":
        ports = [f"{p.get('port')}->{p.get('targetPort')}" for p in spec.get("ports") or []]
        return f"{spec.get('type')} selector={spec.get('selector')} ports={ports}"
    if kind == "endpoints":
        subs = o.get("subsets") or []
        n = sum(len(s.get("addresses") or []) for s in subs)
        return f"{n} ready addresses"
    if kind == "persistentvolumeclaims":
        return str(st.get("phase"))
    if kind == "events":
        return f"{o.get('type')} {o.get('reason')}: {(o.get('message') or '')[:120]}"
    if kind == "configmaps":
        return f"keys={sorted((o.get('data') or {}).keys())}"
    if kind == "secrets":
        return f"keys={sorted((o.get('data') or {}).keys())}"
    return ""


@c.tool
def k8s_list(kind: str, namespace: str, label_selector: str = "", *, apis: Apis | None = None) -> Any:
    apis = apis or Apis()
    c.check_namespace(namespace)
    group, lister, _ = _kind(kind)
    kwargs = {"label_selector": label_selector} if label_selector else {}
    items = _items(getattr(apis.get(group), lister)(namespace, **kwargs))
    key = kind.lower()
    return [
        {"name": (i.get("metadata") or {}).get("name"), "status": c.redact(_summary(key, i))}
        for i in items[:200]
    ]


def _strip(obj: dict) -> dict:
    obj = dict(obj)
    meta = dict(obj.get("metadata") or {})
    meta.pop("managedFields", None)
    obj["metadata"] = meta
    ann = meta.get("annotations") or {}
    ann.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    return obj


@c.tool
def k8s_get(kind: str, namespace: str, name: str, *, apis: Apis | None = None) -> Any:
    apis = apis or Apis()
    c.check_namespace(namespace)
    group, _, reader = _kind(kind)
    obj = _strip(c.plain(getattr(apis.get(group), reader)(name, namespace)))
    if kind.lower() == "secrets":
        meta = obj.get("metadata") or {}
        return {
            "kind": "Secret",
            "metadata": {"name": meta.get("name"), "namespace": meta.get("namespace")},
            "type": obj.get("type"),
            "keys": sorted({*(obj.get("data") or {}), *(obj.get("stringData") or {})}),
        }
    return c.redact_obj(obj)


def _container_state(s: dict | None) -> dict:
    s = s or {}
    for key in ("waiting", "running", "terminated"):
        if s.get(key) is not None:
            return {"state": key, **{k: v for k, v in s[key].items() if v is not None}}
    return {}


@c.tool
def k8s_describe_pod(namespace: str, pod: str, *, apis: Apis | None = None) -> Any:
    apis = apis or Apis()
    c.check_namespace(namespace)
    p = c.plain(apis.core.read_namespaced_pod(pod, namespace))
    spec, st, meta = p.get("spec") or {}, p.get("status") or {}, p.get("metadata") or {}
    by_name = {x.get("name"): x for x in st.get("containerStatuses") or []}
    containers = []
    for ct in spec.get("containers") or []:
        s = by_name.get(ct.get("name"), {})
        containers.append({
            "name": ct.get("name"),
            "image": ct.get("image"),
            "resources": ct.get("resources") or {},
            "restartCount": s.get("restartCount", 0),
            "ready": s.get("ready", False),
            "state": _container_state(s.get("state")),
            "lastState": _container_state(s.get("lastState")),
        })
    out = {
        "name": meta.get("name"),
        "phase": st.get("phase"),
        "reason": st.get("reason"),
        "message": st.get("message"),
        "node": spec.get("nodeName"),
        "owner": [{"kind": o.get("kind"), "name": o.get("name")} for o in meta.get("ownerReferences") or []],
        "conditions": [
            {k: x.get(k) for k in ("type", "status", "reason", "message") if x.get(k)}
            for x in st.get("conditions") or []
        ],
        "containers": containers,
    }
    return c.redact_obj(out)


@c.tool
def k8s_logs(
    namespace: str, pod: str, container: str = "", previous: bool = False, tail: int = 200, *, apis: Apis | None = None
) -> Any:
    apis = apis or Apis()
    c.check_namespace(namespace)
    tail = max(1, min(int(tail), c.MAX_LOG_LINES))
    kwargs: dict[str, Any] = {"tail_lines": tail}
    if container:
        kwargs["container"] = container
    if previous:
        kwargs["previous"] = True
    text = apis.core.read_namespaced_pod_log(pod, namespace, **kwargs)
    return c.safe_text(text or "", tail, tail=True) or "(no log output)"


@c.tool
def k8s_events(namespace: str, name: str = "", *, apis: Apis | None = None) -> Any:
    apis = apis or Apis()
    c.check_namespace(namespace)
    kwargs = {"field_selector": f"involvedObject.name={name}"} if name else {}
    items = _items(apis.core.list_namespaced_event(namespace, **kwargs))

    def stamp(e: dict) -> str:
        return str(e.get("lastTimestamp") or e.get("eventTime") or (e.get("metadata") or {}).get("creationTimestamp") or "")

    items.sort(key=stamp, reverse=True)
    return [
        {
            "type": e.get("type"),
            "reason": e.get("reason"),
            "object": f"{(e.get('involvedObject') or {}).get('kind')}/{(e.get('involvedObject') or {}).get('name')}",
            "message": c.redact(e.get("message") or ""),
            "count": e.get("count") or 1,
            "lastTimestamp": stamp(e),
        }
        for e in items[:MAX_EVENTS]
    ]


def build_server(apis: Apis | None = None) -> MCPServer:
    srv = MCPServer("k8s")

    @srv.tool(name="k8s_list", description="List Kubernetes objects in an allowed namespace with a one-line status each. kind: pods, deployments, replicasets, services, endpoints, configmaps, networkpolicies, events, persistentvolumeclaims.")
    def k8s_list_(kind: str, namespace: str, label_selector: str = "") -> Any:
        return k8s_list(kind, namespace, label_selector, apis=apis)

    @srv.tool(name="k8s_get", description="Get one Kubernetes object as a dict (no managedFields). For kind=secrets only key names are returned, never values.")
    def k8s_get_(kind: str, namespace: str, name: str) -> Any:
        return k8s_get(kind, namespace, name, apis=apis)

    @srv.tool(name="k8s_describe_pod", description="Summarise a pod: phase, conditions, per-container state/lastState/restartCount/image/resources, node, owner.")
    def k8s_describe_pod_(namespace: str, pod: str) -> Any:
        return k8s_describe_pod(namespace, pod, apis=apis)

    @srv.tool(name="k8s_logs", description="Pod logs (redacted, last `tail` lines, max 200). previous=true reads the last terminated container.")
    def k8s_logs_(namespace: str, pod: str, container: str = "", previous: bool = False, tail: int = 200) -> Any:
        return k8s_logs(namespace, pod, container, previous, tail, apis=apis)

    @srv.tool(name="k8s_events", description="Recent events (newest first, max 50) in a namespace, optionally for one object name.")
    def k8s_events_(namespace: str, name: str = "") -> Any:
        return k8s_events(namespace, name, apis=apis)

    return srv


if __name__ == "__main__":
    build_server().run()
