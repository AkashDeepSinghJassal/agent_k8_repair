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
CLASS_HINT = {
    "CrashLoopBackOff": "CRASH_LOOP",
    "ImagePullBackOff": "IMAGE_PULL",
    "ErrImagePull": "IMAGE_PULL",
    "CreateContainerConfigError": "CONFIG_ERROR",
    "OOMKilled": "OOM_KILLED",
    "FailedScheduling": "UNSCHEDULABLE",
    "WorkflowFailed": "STAGE_FAILURE",
}
WORKFLOW_FAILED = ("Failed", "Error")


def is_ignored(body: dict) -> bool:
    return (body.get("metadata", {}).get("labels") or {}).get(IGNORE_LABEL) == "true"


def container_failure(cs: dict) -> dict | None:
    """Failure reason for one ``containerStatuses`` entry, or None if healthy."""
    waiting = (cs.get("state") or {}).get("waiting") or {}
    terminated = (cs.get("lastState") or {}).get("terminated") or {}
    reason = waiting.get("reason")
    if reason not in POD_REASONS:
        reason = "OOMKilled" if terminated.get("reason") == "OOMKilled" else None
    if reason is None:
        return None
    return {
        "container": cs.get("name", ""),
        "reason": reason,
        "exitCode": terminated.get("exitCode"),
        "restartCount": cs.get("restartCount", 0),
        "classHint": CLASS_HINT[reason],
    }


def pod_failures(statuses: list[dict] | None) -> list[dict]:
    return [f for f in (container_failure(cs) for cs in statuses or []) if f]


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


def pod_incident_specs(namespace: str, body: dict, apps_api: Any = None) -> list[dict]:
    """One ``{source, fp, reason, classHint}`` per failing container of a pod."""
    if is_ignored(body):
        return []
    failures = pod_failures((body.get("status") or {}).get("containerStatuses"))
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
    if is_ignored(body) or (body.get("status") or {}).get("phase") not in WORKFLOW_FAILED:
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
    if event.get("type") != "Warning" or event.get("reason") != "FailedScheduling":
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
    for spec in pod_incident_specs(namespace, body, _api("apps")):
        incidents.open_incident(_api("custom"), namespace, spec["source"], spec["fp"])


@kopf.on.event("events", field="reason", value="FailedScheduling")
def scheduling_event(namespace, body, **_):
    pod = None
    obj = body.get("involvedObject") or {}
    if obj.get("kind") == "Pod":
        try:
            pod = _api("core").read_namespaced_pod(obj["name"], namespace).to_dict()
            pod = {"metadata": _snake_meta(pod["metadata"]), "spec": pod.get("spec") or {}}
        except Exception as exc:  # noqa: BLE001
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
