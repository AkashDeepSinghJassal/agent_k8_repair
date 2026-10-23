"""Re-trigger and verify (plan "Re-trigger and verification"). Pure functions; clients are injected.

Flow: refresh the Argo CD Application, wait until it synced the merge SHA, resubmit the failed
workflow (workflow incidents only), then judge the verify window with :func:`evaluate`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .incidents import GROUP

log = logging.getLogger(__name__)

ARGOCD_NS = "argocd"
SYNC_TIMEOUT_SECONDS = 600
VERIFY_TIMEOUT_EXTRA_SECONDS = 600  # a window that never becomes healthy fails after window + this
APP_BY_NAMESPACE = {"demo": "demo-app", "demo-b": "demo-app-b"}
INCIDENT_LABEL = f"{GROUP}/incident"
FAILURE_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError"}


WORKFLOW_APP = "demo-workflows"  # syncs workflows/ (WorkflowTemplates) into ns demo


def app_name(namespace: str, workflow: bool = False) -> str:
    if workflow:
        return WORKFLOW_APP
    return APP_BY_NAMESPACE.get(namespace, "demo-app")


def is_workflow(incident: dict) -> bool:
    return ((incident.get("spec") or {}).get("source") or {}).get("kind") == "Workflow"


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value)).astimezone(UTC)


def iso(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Argo CD ---------------------------------------------------------------


def refresh_app(custom_api: Any, namespace: str, workflow: bool = False) -> None:
    """Ask Argo CD to fetch git now instead of waiting for its poll."""
    custom_api.patch_namespaced_custom_object(
        "argoproj.io", "v1alpha1", ARGOCD_NS, "applications", app_name(namespace, workflow),
        {"metadata": {"annotations": {"argocd.argoproj.io/refresh": "normal"}}},
    )


def get_app(custom_api: Any, namespace: str, workflow: bool = False) -> dict:
    return custom_api.get_namespaced_custom_object(
        "argoproj.io", "v1alpha1", ARGOCD_NS, "applications", app_name(namespace, workflow)
    )


def is_synced(app: dict, merge_sha: str) -> bool:
    sync = (app.get("status") or {}).get("sync") or {}
    return sync.get("status") == "Synced" and sync.get("revision") == merge_sha


def app_healthy(app: dict) -> bool:
    return ((app.get("status") or {}).get("health") or {}).get("status") == "Healthy"


def sync_timed_out(merged_iso: str | None, now: datetime, timeout: int = SYNC_TIMEOUT_SECONDS) -> bool:
    merged = parse_ts(merged_iso)
    return merged is not None and (now - merged).total_seconds() > timeout


# --- re-run ------------------------------------------------------------------


def resubmit(custom_api: Any, ns: str, template: str, incident: str) -> dict:
    wf = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Workflow",
        "metadata": {
            "generateName": f"{template}-repair-",
            "namespace": ns,
            "labels": {INCIDENT_LABEL: incident},
        },
        "spec": {"workflowTemplateRef": {"name": template}},
    }
    return custom_api.create_namespaced_custom_object("argoproj.io", "v1alpha1", ns, "workflows", wf)


def retrigger(incident: dict, custom_api: Any = None, **_: Any) -> dict:
    """Resubmit the failed workflow of a Workflow incident. Returns status fields to merge.

    Deployment incidents (and calls without a client) need no re-run: the sync rolls out new pods.
    """
    source = (incident.get("spec") or {}).get("source") or {}
    if source.get("kind") != "Workflow" or custom_api is None:
        return {}
    meta = incident["metadata"]
    created = resubmit(custom_api, meta["namespace"], source.get("template") or source["name"], meta["name"])
    return {"rerun": {"kind": "Workflow", "name": created["metadata"]["name"]}}


# --- verify window -------------------------------------------------------------


@dataclass
class Verdict:
    state: str  # waiting | healthy | healed | failed
    reason: str = ""
    details: list[str] = field(default_factory=list)
    healthy_since: str | None = None
    restarts: dict[str, int] | None = None  # per-pod restart baseline to persist (deployment checks)


def _pod_problem(pod: dict) -> str | None:
    """A container stuck in a waiting error reason (CrashLoopBackOff, ImagePullBackOff, ...)."""
    for cs in (pod.get("status") or {}).get("containerStatuses") or []:
        waiting = ((cs.get("state") or {}).get("waiting") or {}).get("reason")
        if waiting in FAILURE_WAITING:
            return f"{pod['metadata']['name']}/{cs.get('name')}: {waiting}"
    return None


def pod_restarts(pod: dict) -> int:
    """Total restartCount over all containers of a pod."""
    return sum(cs.get("restartCount", 0) for cs in (pod.get("status") or {}).get("containerStatuses") or [])


def _pod_ready(pod: dict) -> bool:
    conds = (pod.get("status") or {}).get("conditions") or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conds)


def owner_pods(pods: list[dict], owner: str) -> list[dict]:
    """Pods controlled by ``Deployment/x`` (through its ReplicaSets) or ``Pod/x``, not terminating."""
    kind, _, name = owner.partition("/")
    out = []
    for p in pods:
        meta = p.get("metadata") or {}
        if meta.get("deletionTimestamp"):
            continue
        if kind == "Pod":
            if meta.get("name") == name:
                out.append(p)
            continue
        for ref in meta.get("ownerReferences") or []:
            if ref.get("kind") == "ReplicaSet" and ref.get("name", "").rsplit("-", 1)[0] == name:
                out.append(p)
                break
    return out


def evaluate_deployment(
    pods: list[dict], owner: str, app: dict, started: str | None, healthy_since: str | None,
    window: int, now: datetime, baseline: dict[str, int] | None = None,
) -> Verdict:
    """Judge every current pod of ``owner`` (the fix may not recreate pods, e.g. ConfigMap-only repairs).

    Healthy when all pods are Ready, none waits in an error reason, the Application is Healthy and no
    pod's total restartCount changed since ``baseline``. Any change or unready pod resets the clock;
    the window must then pass again. A window that never succeeds fails after ``window`` + timeout.
    """
    start = parse_ts(started) or now
    mine = owner_pods(pods, owner)
    current = {p["metadata"]["name"]: pod_restarts(p) for p in mine}
    restarted = [
        f"{n}: restarts {baseline[n]} -> {c}" for n, c in current.items() if n in (baseline or {}) and baseline[n] != c
    ]
    problems = [m for m in (_pod_problem(p) for p in mine) if m]
    unready = [p["metadata"]["name"] + ": not Ready" for p in mine if not _pod_ready(p)]
    healthy = bool(mine) and not (restarted or problems or unready) and app_healthy(app)
    if not healthy:
        elapsed = (now - start).total_seconds()
        # Only pods created after the merge count: a pre-fix pod still failing is simply waiting to be replaced.
        new_problems = [
            m for p in mine
            if (parse_ts((p.get("metadata") or {}).get("creationTimestamp")) or now) >= start and (m := _pod_problem(p))
        ]
        if new_problems and elapsed > window:
            # A pod created after the merge that sits in ImagePullBackOff / CrashLoopBackOff a full window later
            # means the fix did not hold (or the fault came back). Fail now, so the incident returns to Analyzing
            # instead of absorbing new failures for window + timeout seconds.
            return Verdict("failed", "verify_failed", [*restarted, *problems, *unready], restarts=current)
        if elapsed > window + VERIFY_TIMEOUT_EXTRA_SECONDS:
            details = [*restarted, *problems, *unready] or [f"{len(mine)} pods, app healthy={app_healthy(app)}"]
            return Verdict("failed", "verify_timeout", details, restarts=current)
        return Verdict(
            "waiting", "waiting for pods and Application to become healthy",
            [*restarted, *problems, *unready], restarts=current,
        )
    since = healthy_since or iso(now)
    if (now - (parse_ts(since) or now)).total_seconds() >= window:
        return Verdict("healed", "healthy for the whole window", healthy_since=since, restarts=current)
    return Verdict("healthy", "inside the window", healthy_since=since, restarts=current)


def evaluate_workflow(wf: dict, started: str | None, window: int, now: datetime) -> Verdict:
    phase = (wf.get("status") or {}).get("phase")
    if phase == "Succeeded":
        return Verdict("healed", "re-run succeeded")
    if phase in ("Failed", "Error"):
        msg = (wf.get("status") or {}).get("message") or ""
        return Verdict("failed", f"re-run {phase.lower()}", [msg] if msg else [])
    start = parse_ts(started) or now
    if (now - start).total_seconds() > window + VERIFY_TIMEOUT_EXTRA_SECONDS:
        return Verdict("failed", "verify_timeout", [f"workflow still {phase or 'Pending'}"])
    return Verdict("waiting", f"workflow {phase or 'Pending'}")


def evaluate(
    incident: dict, *, core_api: Any, custom_api: Any, window: int, now: datetime | None = None
) -> Verdict:
    """Judge one tick of the verify window for a Verifying incident."""
    from .evidence import _plain

    now = now or datetime.now(UTC)
    ns = incident["metadata"]["namespace"]
    source = (incident.get("spec") or {}).get("source") or {}
    verify = (incident.get("status") or {}).get("verify") or {}
    started = verify.get("startedAt")
    if source.get("kind") == "Workflow":
        wf = custom_api.get_namespaced_custom_object(
            "argoproj.io", "v1alpha1", ns, "workflows", verify.get("rerun") or ""
        )
        return evaluate_workflow(wf, started, window, now)
    pods = [_plain(p) for p in core_api.list_namespaced_pod(ns).items]
    app = get_app(custom_api, ns)
    return evaluate_deployment(
        pods, source.get("owner") or f"Pod/{source.get('name', '')}", app,
        started, verify.get("healthySince"), window, now, verify.get("restarts"),
    )
