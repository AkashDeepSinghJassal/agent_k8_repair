"""Read-only cluster access for the dashboard.

Parsing is done by pure functions over plain dicts (the JSON shape of k8s objects), so
they are testable without a cluster. ``KubeReader`` is the port; ``KubeClientReader`` is
the real adapter and ``FakeKubeReader`` serves canned data in tests.
"""

from __future__ import annotations

from typing import Any, Protocol

DEFAULT_NAMESPACES = ("demo", "demo-b")
WORKFLOW_NAMESPACE = "argo"
VERSION_LABEL = "app.kubernetes.io/version"


class KubeUnavailable(Exception):
    """Raised when the cluster cannot be reached or configured."""


class KubeReader(Protocol):
    def services(self, namespaces: tuple[str, ...] | list[str]) -> list[dict]: ...
    def argocd_apps(self) -> list[dict]: ...
    def workflows(self, ns: str) -> list[dict]: ...
    def incidents(self) -> list[dict]: ...
    def incident(self, ns: str, name: str) -> dict | None: ...


# ---------------------------------------------------------------- parsing


def _g(d: Any, *path: str, default: Any = None) -> Any:
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return default
        d = d[p]
    return d if d is not None else default


def parse_pod(pod: dict) -> dict:
    statuses = _g(pod, "status", "containerStatuses", default=[]) or []
    restarts = sum(int(c.get("restartCount", 0)) for c in statuses)
    reason = None
    phase = _g(pod, "status", "phase", default="Unknown")
    for c in statuses:
        term = _g(c, "lastState", "terminated")
        if term and term.get("reason"):
            reason = term["reason"]
        waiting = _g(c, "state", "waiting", "reason")
        if waiting:  # CrashLoopBackOff, ImagePullBackOff... are the effective state
            phase = waiting
    return {
        "name": _g(pod, "metadata", "name"),
        "phase": phase,
        "ready": bool(statuses) and all(c.get("ready") for c in statuses),
        "restarts": restarts,
        "lastReason": reason,
    }


def _selector_matches(selector: dict, labels: dict) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


BAD_PHASES = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "Failed", "CreateContainerConfigError"}


def service_status(ready: int, desired: int, pods: list[dict]) -> str:
    """green / amber / red."""
    if (desired and ready == 0) or any(p["phase"] in BAD_PHASES for p in pods):
        return "red"
    if ready < desired or any(not p["ready"] or p["restarts"] > 0 for p in pods):
        return "amber"
    return "green"


def parse_services(deployments: list[dict], pods: list[dict], svcs: list[dict]) -> list[dict]:
    out = []
    for d in deployments:
        meta = d.get("metadata", {})
        ns = meta.get("namespace")
        match = _g(d, "spec", "selector", "matchLabels", default={}) or {}
        mine = [
            parse_pod(p)
            for p in pods
            if _g(p, "metadata", "namespace") == ns
            and _selector_matches(match, _g(p, "metadata", "labels", default={}))
        ]
        tmpl_labels = _g(d, "spec", "template", "metadata", "labels", default={}) or {}
        service = None
        for s in svcs:
            if _g(s, "metadata", "namespace") == ns and _selector_matches(
                _g(s, "spec", "selector", default={}), tmpl_labels
            ):
                service = {
                    "name": _g(s, "metadata", "name"),
                    "type": _g(s, "spec", "type", default="ClusterIP"),
                    "ports": [
                        {"port": p.get("port"), "nodePort": p.get("nodePort"), "protocol": p.get("protocol", "TCP")}
                        for p in _g(s, "spec", "ports", default=[]) or []
                    ],
                }
                break
        containers = _g(d, "spec", "template", "spec", "containers", default=[]) or []
        desired = _g(d, "spec", "replicas", default=1)
        ready = _g(d, "status", "readyReplicas", default=0)
        out.append(
            {
                "name": meta.get("name"),
                "namespace": ns,
                "ready": ready,
                "desired": desired,
                "image": containers[0].get("image") if containers else None,
                "version": _g(meta, "labels", VERSION_LABEL) or _g(tmpl_labels, VERSION_LABEL),
                "pods": mine,
                "service": service,
                "status": service_status(ready, desired, mine),
            }
        )
    return sorted(out, key=lambda s: (s["namespace"] or "", s["name"] or ""))


def parse_argocd_app(app: dict) -> dict:
    revision = _g(app, "status", "sync", "revision", default="")
    return {
        "name": _g(app, "metadata", "name"),
        "namespace": _g(app, "spec", "destination", "namespace"),
        "sync": _g(app, "status", "sync", "status", default="Unknown"),
        "health": _g(app, "status", "health", "status", default="Unknown"),
        "revision": revision,
        "shortRevision": revision[:7],
        "lastSync": _g(app, "status", "operationState", "finishedAt") or _g(app, "status", "reconciledAt"),
    }


def parse_workflow(wf: dict) -> dict:
    nodes = _g(wf, "status", "nodes", default={}) or {}
    failed = None
    for n in nodes.values():
        if n.get("phase") in ("Failed", "Error") and n.get("type") == "Pod" and n.get("message"):
            failed = n["message"]
            break
    if failed is None and _g(wf, "status", "phase") in ("Failed", "Error"):
        failed = _g(wf, "status", "message")
    tmpl = _g(wf, "spec", "workflowTemplateRef", "name") or _g(
        wf, "metadata", "labels", "workflows.argoproj.io/workflow-template"
    )
    return {
        "name": _g(wf, "metadata", "name"),
        "namespace": _g(wf, "metadata", "namespace"),
        "template": tmpl,
        "phase": _g(wf, "status", "phase", default="Pending"),
        "startedAt": _g(wf, "status", "startedAt"),
        "finishedAt": _g(wf, "status", "finishedAt"),
        "failedMessage": failed,
    }


def parse_incident(ri: dict) -> dict:
    st = ri.get("status") or {}
    return {
        "name": _g(ri, "metadata", "name"),
        "namespace": _g(ri, "metadata", "namespace"),
        "phase": st.get("phase", "Detected"),
        "class": _g(st, "classification", "class"),
        "confidence": _g(st, "classification", "confidence"),
        "source": _g(st, "classification", "source"),
        "tier": _g(st, "proposal", "tier"),
        "pullRequest": st.get("pullRequest"),
        "attempts": st.get("attempts", 0),
        "timestamps": st.get("timestamps") or {},
        "outcome": st.get("outcome"),
        "shadow": bool(st.get("shadow", False)),
        "createdAt": _g(ri, "metadata", "creationTimestamp"),
    }


def parse_incident_detail(ri: dict) -> dict:
    d = parse_incident(ri)
    st = ri.get("status") or {}
    d.update(
        {
            "spec": ri.get("spec") or {},
            "signature": st.get("signature"),
            "evidenceRef": st.get("evidenceRef"),
            "proposal": st.get("proposal"),
            "reviewFeedback": st.get("reviewFeedback") or [],
        }
    )
    return d


# ---------------------------------------------------------------- adapters


class KubeClientReader:
    """Real reader using the ``kubernetes`` client. Config is loaded lazily so that
    constructing it (and starting the app) never needs a cluster."""

    def __init__(self, context: str = "kind-repair") -> None:
        self.context = context
        self._ready = False

    def _init(self):
        from kubernetes import client, config

        if not self._ready:
            try:
                config.load_kube_config(context=self.context)
            except Exception:  # noqa: BLE001 - any config failure falls back to in-cluster
                try:
                    config.load_incluster_config()
                except Exception as exc:
                    raise KubeUnavailable(f"no kube config for context {self.context!r} and not in-cluster") from exc
            self._ready = True
        return client

    def _call(self, fn, *a, **kw):
        client = self._init()
        try:
            kw.setdefault("_request_timeout", 5)
            res = fn(*a, **kw)
        except Exception as exc:
            raise KubeUnavailable(str(exc)[:200]) from exc
        return client.ApiClient().sanitize_for_serialization(res)

    def services(self, namespaces):
        client = self._init()
        apps, core = client.AppsV1Api(), client.CoreV1Api()
        dep, pods, svcs = [], [], []
        for ns in namespaces:
            dep += self._call(apps.list_namespaced_deployment, ns).get("items", [])
            pods += self._call(core.list_namespaced_pod, ns).get("items", [])
            svcs += self._call(core.list_namespaced_service, ns).get("items", [])
        return parse_services(dep, pods, svcs)

    def _custom(self, group, version, plural, ns=None):
        api = self._init().CustomObjectsApi()
        if ns:
            return self._call(api.list_namespaced_custom_object, group, version, ns, plural).get("items", [])
        return self._call(api.list_cluster_custom_object, group, version, plural).get("items", [])

    def argocd_apps(self):
        return [parse_argocd_app(a) for a in self._custom("argoproj.io", "v1alpha1", "applications", "argocd")]

    def workflows(self, ns):
        items = [parse_workflow(w) for w in self._custom("argoproj.io", "v1alpha1", "workflows", ns)]
        return sorted(items, key=lambda w: w["startedAt"] or "", reverse=True)[:20]

    def incidents(self):
        items = [parse_incident(i) for i in self._custom("repair.example.dev", "v1alpha1", "repairincidents")]
        return sorted(items, key=lambda i: i["createdAt"] or "", reverse=True)

    def incident(self, ns, name):
        for i in self._custom("repair.example.dev", "v1alpha1", "repairincidents", ns):
            if i["metadata"]["name"] == name:
                return parse_incident_detail(i)
        return None


class FakeKubeReader:
    """In-memory reader for tests. Set ``error`` to simulate an unreachable cluster."""

    def __init__(self, services=None, apps=None, workflows=None, incidents=None, error: str | None = None):
        self._services = services or []
        self._apps = apps or []
        self._workflows = workflows or []
        self._incidents = incidents or []
        self.error = error

    def _check(self):
        if self.error:
            raise KubeUnavailable(self.error)

    def services(self, namespaces):
        self._check()
        return [s for s in self._services if s["namespace"] in namespaces]

    def argocd_apps(self):
        self._check()
        return self._apps

    def workflows(self, ns):
        self._check()
        return [w for w in self._workflows if w.get("namespace", ns) == ns]

    def incidents(self):
        self._check()
        return self._incidents

    def incident(self, ns, name):
        self._check()
        for i in self._incidents:
            if i["namespace"] == ns and i["name"] == name:
                return i
        return None
