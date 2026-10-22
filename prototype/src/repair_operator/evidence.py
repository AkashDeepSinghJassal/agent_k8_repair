"""Evidence bundle: collection, redaction, validation, storage (CONTRACTS section 6).

All Kubernetes / GitHub access goes through injected clients so the logic is
testable with fakes. ``core_api`` is a ``CoreV1Api``, ``custom_api`` a
``CustomObjectsApi`` and ``github`` a PyGithub ``Repository`` (or None).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

from . import incidents
from .config import get_settings
from .fingerprint import knowledge_signature
from .models import Phase

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "evidence.v1.json"
SOURCE_PATH_ANNOTATION = "repair.example.dev/source-path"
MAX_EVENTS = 20
MAX_LOG_LINES = 200
MAX_FILE_LINES = 400
MAX_ERROR_LINES = 20
MAX_AVAILABLE = 100
ERROR_RE = re.compile(r"(?i)(error|fail|fatal|exception|panic|not found|denied|refused|exit)")

# --- redaction -------------------------------------------------------------

_VALUE = r"""(?:"[^"]*"|'[^']*'|\S+)"""
REDACTIONS = [
    re.compile(r"(?i)[\w.-]*(?:password|passwd|secret|token|api[_-]?key)[\w.-]*[\"']?\s*[:=]\s*" + _VALUE),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL),
    re.compile(r"(?i)\bbearer\s+[\w.~+/=-]{8,}"),
]


def redact(text: str) -> str:
    """Replace secrets (key=value, AWS keys, JWTs, PEM blocks, bearer tokens) with ``[REDACTED]``."""
    for rx in REDACTIONS:
        text = rx.sub("[REDACTED]", text)
    return text


def _redact_lines(lines: list[str]) -> list[str]:
    """Redact as one text so multi-line PEM blocks are caught."""
    return redact("\n".join(lines)).split("\n") if lines else []


def _tail(lines: list[str], n: int) -> list[str]:
    return lines[-n:] if len(lines) > n else lines


def _head_text(text: str, n: int = MAX_FILE_LINES) -> str:
    lines = text.splitlines()
    return "\n".join(lines[:n]) if len(lines) > n else text


# --- helpers ----------------------------------------------------------------


def _plain(obj: Any) -> dict:
    """Kubernetes client model or dict -> camelCase dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    from kubernetes import client

    return client.ApiClient().sanitize_for_serialization(obj) or {}


def _status(exc: Exception) -> int | None:
    return getattr(exc, "status", None)


def _ns_name(incident: dict) -> tuple[str, str]:
    return incident["metadata"]["namespace"], incident["metadata"]["name"]


def _read_logs(core_api: Any, ns: str, pod: str, container: str | None, previous: bool = False) -> list[str]:
    kwargs: dict[str, Any] = {"tail_lines": MAX_LOG_LINES}
    if container:
        kwargs["container"] = container
    if previous:
        kwargs["previous"] = True
    try:
        text = core_api.read_namespaced_pod_log(pod, ns, **kwargs)
    except Exception as exc:  # noqa: BLE001  (no previous container, pod gone, ...)
        log.info("no %s logs for %s/%s: %s", "previous" if previous else "current", ns, pod, exc)
        return []
    return _tail((text or "").splitlines(), MAX_LOG_LINES)


# --- pod ----------------------------------------------------------------------


def pod_recovered(pod: dict, container: str) -> bool:
    """Ready, and the source container is not waiting in a failure state."""
    status = pod.get("status") or {}
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])
    if not ready:
        return False
    for cs in status.get("containerStatuses") or []:
        if container and cs.get("name") != container:
            continue
        if (cs.get("state") or {}).get("waiting") or not cs.get("ready", True):
            return False
    return True


def _container_failure(pod: dict, container: str, fallback_reason: str) -> dict:
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    cs = next((c for c in statuses if c.get("name") == container), statuses[0] if statuses else {})
    state = cs.get("state") or {}
    last = (cs.get("lastState") or {}).get("terminated") or state.get("terminated") or {}
    waiting = state.get("waiting") or {}
    return {
        "reason": fallback_reason or waiting.get("reason") or last.get("reason") or "Unknown",
        "exitCode": last.get("exitCode"),
        "restartCount": cs.get("restartCount", 0),
        "message": redact(waiting.get("message") or last.get("message") or "") or None,
    }


def _env_from(container: dict) -> list[str]:
    out = []
    for ef in container.get("envFrom") or []:
        if ef.get("configMapRef"):
            out.append(f"configmap/{ef['configMapRef'].get('name', '')}")
        elif ef.get("secretRef"):
            out.append(f"secret/{ef['secretRef'].get('name', '')}")
    return out


def _spec_section(pod_spec: dict, container: str) -> dict:
    containers = pod_spec.get("containers") or []
    c = next((x for x in containers if x.get("name") == container), containers[0] if containers else {})
    return {
        "image": c.get("image"),
        "resources": c.get("resources") or {},
        "env": [e["name"] for e in c.get("env") or [] if e.get("name")],  # names only, never values
        "envFrom": _env_from(c),
    }


def _events(core_api: Any, ns: str, pod_name: str) -> list[dict]:
    try:
        resp = core_api.list_namespaced_event(ns, field_selector=f"involvedObject.name={pod_name}")
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot list events for %s/%s: %s", ns, pod_name, exc)
        return []
    raw = resp.get("items") if isinstance(resp, dict) else getattr(resp, "items", None)
    items = [e for e in (_plain(e) for e in raw or []) if e.get("type") == "Warning"]

    def stamp(e: dict) -> str:
        meta = e.get("metadata") or {}
        return str(e.get("lastTimestamp") or e.get("eventTime") or meta.get("creationTimestamp") or "")

    items.sort(key=stamp, reverse=True)
    return [
        {"reason": e.get("reason"), "message": redact(e.get("message") or ""), "count": e.get("count") or 1}
        for e in items[:MAX_EVENTS]
    ]


# --- workflow -----------------------------------------------------------------


def _workflow_section(core_api: Any, custom_api: Any, ns: str, source: dict) -> tuple[dict, dict, dict]:
    """Returns (workflow section, failure, workflow metadata)."""
    wf = custom_api.get_namespaced_custom_object("argoproj.io", "v1alpha1", ns, "workflows", source["name"])
    nodes = (wf.get("status") or {}).get("nodes") or {}
    pod_nodes = [n for n in nodes.values() if n.get("type") == "Pod"]
    node = next((n for n in pod_nodes if n.get("displayName") == source.get("node")), None)
    if node is None:
        node = next((n for n in pod_nodes if n.get("phase") in ("Failed", "Error")), {})
    pod_name = node.get("id") or ""
    step_logs = _redact_lines(_read_logs(core_api, ns, pod_name, "main")) if pod_name else []
    message = redact(node.get("message") or (wf.get("status") or {}).get("message") or "")
    code = (node.get("outputs") or {}).get("exitCode")
    try:
        exit_code = int(code) if code is not None else None
    except (TypeError, ValueError):
        exit_code = None
    section = {
        "name": source["name"],
        "template": node.get("templateName") or source.get("template") or "",
        "node": node.get("displayName") or source.get("node") or "",
        "phase": node.get("phase"),
        "message": message,
        "logs": step_logs,
    }
    failure = {"reason": "WorkflowFailed", "exitCode": exit_code, "restartCount": 0, "message": message or None}
    return section, failure, wf


# --- git ----------------------------------------------------------------------


def _git_section(github: Any, path: str | None, branch: str) -> dict | None:
    if github is None or not path:
        return None
    try:
        content = github.get_contents(path, ref=branch)
        text = content.decoded_content.decode("utf-8", "replace")
        sha = content.sha
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot read %s from GitHub: %s", path, exc)
        return None
    diff = ""
    try:
        commit = github.get_commits(path=path, sha=branch)[0]
        parts = [
            f"--- a/{path}\n+++ b/{path}\n{f.patch}"
            for f in commit.files
            if f.filename == path and getattr(f, "patch", None)
        ]
        diff = "\n".join(parts)
        sha = commit.sha or sha
    except Exception as exc:  # noqa: BLE001
        log.info("no commit diff for %s: %s", path, exc)
    return {
        "repo": getattr(github, "full_name", "") or "",
        "path": path,
        "sha": sha,
        "lastCommitDiff": redact(_head_text(diff)),
        "file": redact(_head_text(text)),
    }


# --- bundle -------------------------------------------------------------------


def error_lines(*streams: list[str]) -> list[str]:
    """Distinct lines matching ERROR_RE across the streams; the last 20."""
    seen: dict[str, None] = {}
    for stream in streams:
        for line in stream:
            if line.strip() and ERROR_RE.search(line):
                seen[line.strip()] = None
    return list(seen)[-MAX_ERROR_LINES:]


def _available(core_api: Any, ns: str) -> dict[str, list[str]]:
    """Names (never data) of ConfigMaps and Secrets in the namespace; empty lists on error."""
    out: dict[str, list[str]] = {"configmaps": [], "secrets": []}
    for key, method in (("configmaps", "list_namespaced_config_map"), ("secrets", "list_namespaced_secret")):
        try:
            resp = getattr(core_api, method)(ns)
            raw = resp.get("items") if isinstance(resp, dict) else getattr(resp, "items", None)
            names = [(_plain(i).get("metadata") or {}).get("name") for i in raw or []]
            out[key] = sorted(n for n in names if n)[:MAX_AVAILABLE]
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot list %s in %s: %s", key, ns, exc)
    return out


def collect(core_api: Any, custom_api: Any, github: Any, incident: dict, *, now: datetime | None = None) -> dict:
    """Build the redacted evidence bundle for an incident (a RepairIncident dict)."""
    ns, name = _ns_name(incident)
    source = (incident.get("spec") or {}).get("source") or {}
    branch = get_settings().demo_branch
    events: list[dict] = []
    logs: dict[str, list[str]] = {"current": [], "previous": []}
    workflow = None

    if source.get("kind") == "Workflow":
        workflow, failure, wf_obj = _workflow_section(core_api, custom_api, ns, source)
        path = ((wf_obj.get("metadata") or {}).get("annotations") or {}).get(SOURCE_PATH_ANNOTATION)
        if not path:
            template = ((wf_obj.get("spec") or {}).get("workflowTemplateRef") or {}).get("name") or source.get("template")
            path = f"workflows/{template}.yaml" if template else None
        spec = {"image": None, "resources": {}, "env": [], "envFrom": []}
        src = {
            "kind": "Workflow",
            "name": source["name"],
            "namespace": ns,
            "owner": f"WorkflowTemplate/{workflow['template']}",
            "container": workflow["node"],
        }
        step_type, texts = "workflow", [workflow["logs"], [workflow["message"]]]
    else:
        pod_name, container = source.get("name", ""), source.get("container", "")
        pod: dict = {}
        try:
            pod = _plain(core_api.read_namespaced_pod(pod_name, ns))
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot read pod %s/%s: %s", ns, pod_name, exc)
        failure = _container_failure(pod, container, source.get("reason", ""))
        if not failure["message"] and source.get("message"):
            failure["message"] = redact(source["message"])
        spec = _spec_section(pod.get("spec") or {}, container)
        path = ((pod.get("metadata") or {}).get("annotations") or {}).get(SOURCE_PATH_ANNOTATION)
        events = _events(core_api, ns, pod_name)
        if container:
            logs["current"] = _redact_lines(_read_logs(core_api, ns, pod_name, container))
            logs["previous"] = _redact_lines(_read_logs(core_api, ns, pod_name, container, previous=True))
        src = {
            "kind": "Pod",
            "name": pod_name,
            "namespace": ns,
            "owner": source.get("owner") or f"Pod/{pod_name}",
            "container": container,
        }
        step_type = "pod"
        texts = [logs["current"], logs["previous"], [e["message"] for e in events], [failure["message"] or ""]]

    sig = knowledge_signature(failure["reason"], failure["exitCode"], step_type, error_lines(*texts))
    return {
        "schema": "evidence/v1",
        "incident": name,
        "collectedAt": incidents.utc_iso(now),
        "signature": sig,
        "source": src,
        "failure": failure,
        "spec": spec,
        "events": events,
        "logs": logs,
        "workflow": workflow,
        "git": _git_section(github, path, branch),
        "available": _available(core_api, ns),
        "history": [],
    }


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))


def validate(bundle: dict) -> None:
    """Raise ``jsonschema.ValidationError`` when the bundle violates evidence.v1.json."""
    _validator().validate(bundle)


def store(bundle: dict, attempt: int, state_dir: Path | None = None) -> str:
    """Validate, write ``<state_dir>/evidence/<incident>-<attempt>.json``, return ``sha256:<hex>``."""
    validate(bundle)
    base = Path(state_dir if state_dir is not None else get_settings().state_dir).expanduser() / "evidence"
    base.mkdir(parents=True, exist_ok=True)
    data = json.dumps(bundle, indent=2, sort_keys=True).encode()
    (base / f"{bundle['incident']}-{attempt}.json").write_bytes(data)
    return "sha256:" + hashlib.sha256(data).hexdigest()


def collect_and_store(
    core_api: Any,
    custom_api: Any,
    github: Any,
    incident: dict,
    *,
    now: datetime | None = None,
    state_dir: Path | None = None,
) -> dict:
    """Collect, validate and store; returns ``{"evidenceRef", "signature"}``."""
    attempt = ((incident.get("status") or {}).get("attempts") or 0) + 1
    bundle = collect(core_api, custom_api, github, incident, now=now)
    ref = store(bundle, attempt, state_dir)
    return {"evidenceRef": ref, "signature": bundle["signature"]}


def recovered(core_api: Any, incident: dict) -> str | None:
    """Reason when a Pod incident healed itself during the debounce window, else None."""
    ns, _ = _ns_name(incident)
    source = (incident.get("spec") or {}).get("source") or {}
    if source.get("kind") != "Pod" or source.get("reason") == "FailedScheduling":
        return None
    try:
        pod = _plain(core_api.read_namespaced_pod(source.get("name", ""), ns))
    except Exception as exc:  # noqa: BLE001
        return "pod no longer exists" if _status(exc) == 404 else None
    if pod_recovered(pod, source.get("container", "")):
        return "pod recovered within the debounce window"
    return None


def default_clients() -> tuple[Any, Any, Any]:
    """(core_api, custom_api, github repo or None) from the environment."""
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except Exception:  # noqa: BLE001
        config.load_kube_config()
    settings = get_settings()
    repo = None
    if settings.github_token:
        from github import Auth, Github

        repo = Github(auth=Auth.Token(settings.github_token)).get_repo(settings.demo_repo)
    return client.CoreV1Api(), client.CustomObjectsApi(), repo


def handle_collect(
    body: dict, patch: dict, clients: tuple[Any, Any, Any] | None = None, now: datetime | None = None
) -> None:
    """Detected -> Collected, or Dismissed when the pod recovered. Writes into ``patch``."""
    core_api, custom_api, github = clients if clients is not None else default_clients()
    reason = recovered(core_api, body)
    if reason:
        incidents.set_phase(patch, Phase.DISMISSED, now, outcome={"result": "dismissed", "reason": reason})
        return
    result = collect_and_store(core_api, custom_api, github, body, now=now)
    incidents.set_phase(patch, Phase.COLLECTED, now, **result)
