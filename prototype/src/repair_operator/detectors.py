"""Detectors: turn raw pod / event / workflow state into RepairIncidents.

Pure functions hold the logic (testable without a cluster); the Kopf handlers
at the bottom are thin wrappers.
"""

from __future__ import annotations

import logging
from typing import Any

import kopf

from . import incidents
from .fingerprint import dedupe_fingerprint
from .incidents import GROUP, PLURAL, VERSION
from .models import Phase

log = logging.getLogger(__name__)

IGNORE_LABEL = f"{GROUP}/ignore"
INCIDENT_LABEL = f"{GROUP}/incident"

POD_REASONS = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError"}
# The loop repairs the demo workloads, not the platform it runs on (a restarting Argo CD pod is not an incident).
IGNORED_NAMESPACES = frozenset({"argocd", "argo", "argo-events", "kube-system", "kube-public", "local-path-storage", "repair-system"})
PROBE_RESTARTS = 3  # restarts of a still-Running container before it counts as a liveness-probe incident
PROBE_REASON = "LivenessProbeRestarts"
CLASS_HINT = {
    "CrashLoopBackOff": "CRASH_LOOP",
    "ImagePullBackOff": "IMAGE_PULL",
    "ErrImagePull": "IMAGE_PULL",
    "CreateContainerConfigError": "CONFIG_ERROR",
    PROBE_REASON: "CRASH_LOOP",  # F13 / F19 are catalogued as CrashLoopBackOff faults; no new class
    "OOMKilled": "OOM_KILLED",
    "FailedScheduling": "UNSCHEDULABLE",
    "WorkflowFailed": "STAGE_FAILURE",
}
WORKFLOW_FAILED = ("Failed", "Error")


def is_ignored(body: dict) -> bool:
    return (body.get("metadata", {}).get("labels") or {}).get(IGNORE_LABEL) == "true"


def probe_failure_message(events: list[dict] | None) -> str:
    """Message of the newest "Liveness probe failed" / "failed liveness probe, will be restarted" event, or ''."""
    msgs = [e for e in events or [] if "liveness probe" in (e.get("message") or "").lower()]
    msgs.sort(key=lambda e: str(e.get("lastTimestamp") or e.get("eventTime") or ""))
    return (msgs[-1].get("message") or "") if msgs else ""


def container_failure(cs: dict, probe_message: str = "") -> dict | None:
    """Failure reason for one ``containerStatuses`` entry, or None if healthy.

    A container that is ``Running`` but keeps restarting (liveness probe) is a failure from the third restart on,
    before it ever reaches CrashLoopBackOff, when the last termination (or a probe event) shows why.
    """
    waiting = (cs.get("state") or {}).get("waiting") or {}
    terminated = (cs.get("lastState") or {}).get("terminated") or {}
    reason = waiting.get("reason")
    if reason not in POD_REASONS:
        reason = "OOMKilled" if terminated.get("reason") == "OOMKilled" else None
    if reason is None and cs.get("restartCount", 0) >= PROBE_RESTARTS and (terminated or probe_message):
        reason = PROBE_REASON
    if reason is None:
        return None
    return {
        "container": cs.get("name", ""),
        "reason": reason,
        "exitCode": terminated.get("exitCode"),
        "restartCount": cs.get("restartCount", 0),
        "classHint": CLASS_HINT[reason],
    }


def pod_failures(statuses: list[dict] | None, probe_message: str = "") -> list[dict]:
    return [f for f in (container_failure(cs, probe_message) for cs in statuses or []) if f]


def resolve_owner(body: dict, apps_api: Any = None) -> str:
    """Pod -> ReplicaSet -> Deployment. Returns ``Kind/name`` (falls back to the pod itself)."""
    meta = body.get("metadata", {})
    refs = meta.get("ownerReferences") or []
    ref = next((r for r in refs if r.get("controller")), refs[0] if refs else None)
    if ref is None:
        return f"Pod/{meta.get('name', '')}"
    kind, name = ref["kind"], ref["name"]
    if kind == "ReplicaSet":
        owner = None
        if apps_api is not None:
            try:
                rs = apps_api.read_namespaced_replica_set(name, meta.get("namespace"))
                rs_refs = getattr(rs.metadata, "owner_references", None) or []
                owner = next((r for r in rs_refs if r.kind == "Deployment"), None)
            except Exception as exc:  # noqa: BLE001
                log.warning("cannot read ReplicaSet %s: %s", name, exc)
        if owner is not None:
            return f"Deployment/{owner.name}"
        # fallback: strip the pod-template-hash suffix of the ReplicaSet name
        return f"Deployment/{name.rsplit('-', 1)[0]}" if "-" in name else f"ReplicaSet/{name}"
    return f"{kind}/{name}"


def pod_incident_specs(namespace: str, body: dict, apps_api: Any = None, events: list[dict] | None = None) -> list[dict]:
    """One ``{source, fp, reason, classHint}`` per failing container of a pod (``events``: the pod's recent events)."""
    if is_ignored(body) or namespace in IGNORED_NAMESPACES:
        return []
    probe_msg = probe_failure_message(events)
    failures = pod_failures((body.get("status") or {}).get("containerStatuses"), probe_msg)
    if not failures:
        return []
    owner = resolve_owner(body, apps_api)
    name = body["metadata"]["name"]
    return [
        {
            "source": {
                "kind": "Pod",
                "name": name,
                "owner": owner,
                "container": f["container"],
                "node": (body.get("spec") or {}).get("nodeName"),
                "reason": f["reason"],
                "classHint": f["classHint"],
                **({"message": probe_msg} if f["reason"] == PROBE_REASON and probe_msg else {}),
                **({"restartCount": f["restartCount"]} if f["reason"] == PROBE_REASON else {}),
            },
            "fp": dedupe_fingerprint(namespace, owner, f["container"], f["reason"]),
        }
        for f in failures
    ]


def workflow_failed_node(body: dict) -> str:
    nodes = (body.get("status") or {}).get("nodes") or {}
    failed = [n for n in nodes.values() if n.get("phase") in WORKFLOW_FAILED and n.get("type") == "Pod"]
    return failed[0].get("displayName", "unknown") if failed else "unknown"


def workflow_incident_spec(namespace: str, body: dict) -> dict | None:
    """Incident spec for a failed workflow, or None when ignored / not failed."""
    if is_ignored(body) or namespace in IGNORED_NAMESPACES or (body.get("status") or {}).get("phase") not in WORKFLOW_FAILED:
        return None
    name = body["metadata"]["name"]
    template = ((body.get("spec") or {}).get("workflowTemplateRef") or {}).get("name") or (
        (body["metadata"].get("labels") or {}).get("workflows.argoproj.io/workflow-template") or name
    )
    node = workflow_failed_node(body)
    return {
        "source": {
            "kind": "Workflow",
            "name": name,
            "template": template,
            "node": node,
            "classHint": CLASS_HINT["WorkflowFailed"],
        },
        "fp": dedupe_fingerprint(namespace, template, node, "WorkflowFailed"),
    }


def event_incident_spec(namespace: str, event: dict, pod_body: dict | None, apps_api: Any = None) -> dict | None:
    """Only Warning/FailedScheduling events on pods open an incident."""
    if event.get("type") != "Warning" or event.get("reason") != "FailedScheduling" or namespace in IGNORED_NAMESPACES:
        return None
    obj = event.get("involvedObject") or {}
    if obj.get("kind") != "Pod" or (pod_body and is_ignored(pod_body)):
        return None
    pod_name = obj.get("name", "")
    owner = resolve_owner(pod_body, apps_api) if pod_body else f"Pod/{pod_name}"
    return {
        "source": {
            "kind": "Pod",
            "name": pod_name,
            "owner": owner,
            "container": "",
            "reason": "FailedScheduling",
            "classHint": CLASS_HINT["FailedScheduling"],
            "message": event.get("message", ""),
        },
        "fp": dedupe_fingerprint(namespace, owner, "", "FailedScheduling"),
    }


def route_workflow_failure(api: Any, namespace: str, incident: str, workflow: str) -> bool:
    """A workflow carrying the incident label failed. Returns True when it is the live re-run.

    The verify tick (``verify_handlers.verify_step``) is the single owner of the transition: it sees
    the failed workflow, writes the one feedback entry and moves the incident on. This detector
    only logs, and does nothing for late failures, other workflows or terminal incidents.
    """
    try:
        inc = api.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, incident)
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot read incident %s for failed workflow %s: %s", incident, workflow, exc)
        return False
    status = inc.get("status") or {}
    if status.get("phase") != Phase.VERIFYING.value or (status.get("verify") or {}).get("rerun") != workflow:
        log.info("workflow %s failed but incident %s is not waiting for it, ignoring", workflow, incident)
        return False
    log.info("re-run %s of incident %s failed; the verify tick will handle it", workflow, incident)
    return True


# --- cluster clients (lazy, so importing this module needs no kubeconfig) ---

_clients: dict[str, Any] = {}


def _api(kind: str) -> Any:
    if not _clients:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except Exception:  # noqa: BLE001
            config.load_kube_config()
        _clients.update(custom=client.CustomObjectsApi(), apps=client.AppsV1Api(), core=client.CoreV1Api())
    return _clients[kind]


# --- thin Kopf handlers ---


@kopf.on.field("pods", field="status.containerStatuses")
def pod_failed(namespace, body, **_):
    events = None
    if any(c.get("restartCount", 0) >= PROBE_RESTARTS for c in (body.get("status") or {}).get("containerStatuses") or []):
        try:  # probe events explain restarts of a Running container; best effort
            sel = f"involvedObject.name={body['metadata']['name']},involvedObject.kind=Pod"
            events = [e.to_dict() for e in _api("core").list_namespaced_event(namespace, field_selector=sel).items]
            events = [{"message": e.get("message"), "lastTimestamp": str(e.get("last_timestamp") or "")} for e in events]
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot list events of pod %s: %s", body["metadata"].get("name"), exc)
    for spec in pod_incident_specs(namespace, body, _api("apps"), events):
        incidents.open_incident(_api("custom"), namespace, spec["source"], spec["fp"])


@kopf.on.event("events", field="reason", value="FailedScheduling")
def scheduling_event(namespace, body, **_):
    pod = None
    obj = body.get("involvedObject") or {}
    if obj.get("kind") == "Pod":
        try:
            pod = _api("core").read_namespaced_pod(obj["name"], namespace).to_dict()
            if (pod.get("spec") or {}).get("node_name"):
                return  # stale event: the pod has been scheduled since
            pod = {"metadata": _snake_meta(pod["metadata"]), "spec": pod.get("spec") or {}}
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) == 404:
                return  # stale event: the pod is gone
            log.warning("cannot read pod %s: %s", obj.get("name"), exc)
    spec = event_incident_spec(namespace, dict(body), pod, _api("apps"))
    if spec:
        incidents.open_incident(_api("custom"), namespace, spec["source"], spec["fp"])


def _snake_meta(meta: dict) -> dict:
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "labels": meta.get("labels"),
        "ownerReferences": [
            {"kind": r["kind"], "name": r["name"], "controller": r.get("controller")}
            for r in meta.get("owner_references") or []
        ],
    }


@kopf.on.field("workflows.argoproj.io", field="status.phase")
def workflow_failed(namespace, name, body, new, **_):
    if new not in WORKFLOW_FAILED:
        return
    incident = (body["metadata"].get("labels") or {}).get(INCIDENT_LABEL)
    if incident:
        route_workflow_failure(_api("custom"), namespace, incident, name)
        return
    spec = workflow_incident_spec(namespace, body)
    if spec:
        incidents.open_incident(_api("custom"), namespace, spec["source"], spec["fp"])
