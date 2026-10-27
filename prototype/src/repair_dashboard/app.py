"""Read-only dashboard API + static UI. Run: uvicorn repair_dashboard.app:app --port 8090"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from functools import lru_cache
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from repair_faults import injector as faults

from . import actions
from .actions import FaultBackend, FaultState, GitHubPRs, GitHubUnavailable, PRClient
from .kube import DEFAULT_NAMESPACES, WORKFLOW_NAMESPACES, KubeClientReader, KubeReader, KubeUnavailable

STATIC = Path(__file__).parent / "static"
CONTEXT = os.environ.get("REPAIR_KUBE_CONTEXT", "kind-repair")

app = FastAPI(title="Repair loop dashboard")


@lru_cache
def _reader() -> KubeReader:
    return KubeClientReader(CONTEXT)


def get_reader() -> KubeReader:
    return _reader()


@app.exception_handler(KubeUnavailable)
async def _unavailable(_request, exc: KubeUnavailable):
    return JSONResponse(status_code=503, content={"error": "cluster unavailable", "detail": str(exc)})


@app.exception_handler(GitHubUnavailable)
async def _gh_unavailable(_request, exc: GitHubUnavailable):
    return JSONResponse(status_code=503, content={"error": "github unavailable", "detail": str(exc)})


@lru_cache
def _prs() -> PRClient:
    return GitHubPRs()


def get_prs() -> PRClient:
    return _prs()


@lru_cache
def _backend() -> FaultBackend:
    state_dir = Path(os.environ.get("REPAIR_STATE_DIR") or "~/.repair-loop").expanduser()
    return FaultBackend(state=FaultState(state_dir / "dashboard-faults.json"))


def get_backend() -> FaultBackend:
    return _backend()


Reader = Depends(get_reader)
PRs = Depends(get_prs)
Backend = Depends(get_backend)
RELEASE_TEMPLATE = faults.DEFAULT_WORKFLOW_TEMPLATE
RERUN_NAMESPACE = "demo"
JsonBody = Body(default_factory=dict)


@app.get("/api/health")
def health(reader: KubeReader = Reader):
    try:
        reader.argocd_apps()
        return {"status": "ok", "cluster": True, "context": CONTEXT}
    except KubeUnavailable as exc:
        return {"status": "ok", "cluster": False, "context": CONTEXT, "detail": str(exc)}


@app.get("/api/services")
def services(reader: KubeReader = Reader):
    return reader.services(list(DEFAULT_NAMESPACES))


@app.get("/api/argocd")
def argocd(reader: KubeReader = Reader):
    return reader.argocd_apps()


@app.get("/api/workflows")
def workflows(reader: KubeReader = Reader):
    return reader.workflows(list(WORKFLOW_NAMESPACES))


@app.get("/api/incidents")
def incidents(reader: KubeReader = Reader):
    return reader.incidents()


@app.get("/api/incidents/{ns}/{name}")
def incident(ns: str, name: str, reader: KubeReader = Reader):
    found = reader.incident(ns, name)
    if found is None:
        raise HTTPException(404, "incident not found")
    return found


def _fault_error(exc: faults.FaultError) -> HTTPException:
    msg = str(exc)
    if msg.startswith("unknown fault"):
        return HTTPException(404, msg)
    if "REPAIR_GITHUB_TOKEN" in msg:
        return HTTPException(503, actions.TOKEN_MESSAGE)
    return HTTPException(502, msg)


def _linked_incident(entry: dict, since: str | None, reader: KubeReader) -> str | None:
    if not since:
        return None
    try:
        items = reader.incidents()
    except KubeUnavailable:
        return None
    hits = [i for i in items if i.get("namespace") == entry["namespace"] and (i.get("createdAt") or "") >= since]
    hits.sort(key=lambda i: i.get("createdAt") or "")
    return hits[0]["name"] if hits else None


def _fault_view(entry: dict, backend: FaultBackend, reader: KubeReader) -> dict:
    since = backend.state.injected.get(entry["id"])
    if since is not None and entry.get("target_file"):
        # The mark is only a note from when Inject was clicked. If the repo no longer carries the fault
        # (the repair loop's PR was merged, or someone fixed it by hand) the fault is gone: drop the mark.
        try:
            if not _is_injected_in_git(entry, backend):
                backend.state.clear(entry["id"])
                since = None
        except Exception as exc:  # noqa: BLE001 - GitHub unreachable or no token: keep showing the mark
            logging.getLogger(__name__).debug("fault %s: git check skipped: %s", entry["id"], exc)
    keys = ("id", "title", "description", "namespace", "kind", "expected_class", "expected_outcome", "correct_fix")
    view = {k: entry[k] for k in keys}
    view.update(injected=since is not None, injectedAt=since, incident=_linked_incident(entry, since, reader))
    return view


def _is_injected_in_git(entry: dict, backend: FaultBackend) -> bool:
    """True when the target file already carries the breaking edits (survives a dashboard restart)."""
    git = backend.git or faults._default_git()
    return faults.is_injected(entry, git.get_file(entry["target_file"]))


@app.get("/api/actions/status")
def actions_status():
    has_token = bool(os.environ.get("REPAIR_GITHUB_TOKEN"))
    return {
        "github": has_token,
        "repo": actions.demo_repo(),
        "approver": actions.approver(),
        "message": None if has_token else actions.TOKEN_MESSAGE,
    }


@app.get("/api/faults")
def list_faults(backend: FaultBackend = Backend, reader: KubeReader = Reader):
    try:
        return [_fault_view(e, backend, reader) for e in faults.list_faults()]
    except faults.FaultError as exc:
        raise _fault_error(exc) from exc


def _argocd_app_for(entry: dict) -> str | None:
    """The Argo CD Application that syncs a fault's target file (None for cluster-only faults)."""
    path = entry.get("target_file") or ""
    if path.startswith("deploy/demo-b/"):
        return "demo-app-b"
    if path.startswith("deploy/demo/"):
        return "demo-app"
    if path.startswith("workflows/"):
        return "demo-workflows"
    return None


def _refresh_argocd(entry: dict, reader: KubeReader) -> bool:
    """Ask Argo CD to re-read git now instead of waiting for its poll (about 2 minutes). Best effort."""
    app_name = _argocd_app_for(entry)
    if app_name is None:
        return False
    try:
        reader.refresh_argocd_app(app_name)
        return True
    except Exception as exc:  # noqa: BLE001 - the commit succeeded; Argo CD will still pick it up by polling
        logging.getLogger(__name__).warning("argocd refresh of %s failed: %s", app_name, exc)
        return False


@app.post("/api/faults/{fault_id}/inject")
def inject_fault(fault_id: str, backend: FaultBackend = Backend, reader: KubeReader = Reader):
    try:
        entry = faults.get_fault(fault_id)
        with backend.state.lock:
            if entry["kind"] == "cluster":
                if fault_id in backend.state.injected:
                    return {"id": fault_id, "status": "already-injected", "injectedAt": backend.state.injected[fault_id]}
            elif _is_injected_in_git(entry, backend):
                return {"id": fault_id, "status": "already-injected", "injectedAt": backend.state.mark(fault_id)}
            result = faults.inject(fault_id, git=backend.git, kubectl=backend.kubectl)
            refreshed = _refresh_argocd(entry, reader)
            return {**result, "status": "injected", "injectedAt": backend.state.mark(fault_id), "argocdRefreshed": refreshed}
    except faults.FaultError as exc:
        raise _fault_error(exc) from exc


@app.post("/api/faults/{fault_id}/revert")
def revert_fault(fault_id: str, backend: FaultBackend = Backend, reader: KubeReader = Reader):
    try:
        entry = faults.get_fault(fault_id)
        with backend.state.lock:
            if entry["kind"] != "cluster" and not _is_injected_in_git(entry, backend):
                backend.state.clear(fault_id)  # git is the truth; a repair may have healed it already
                return {"id": fault_id, "status": "not-injected"}
            result = faults.revert(fault_id, git=backend.git, kubectl=backend.kubectl)
            backend.state.clear(fault_id)
            refreshed = _refresh_argocd(entry, reader)
            return {**result, "status": "reverted", "argocdRefreshed": refreshed}
    except faults.FaultError as exc:
        raise _fault_error(exc) from exc


def _incident_pr(ns: str, name: str, reader: KubeReader) -> tuple[dict, int]:
    found = reader.incident(ns, name)
    if found is None:
        raise HTTPException(404, "incident not found")
    try:
        return found, actions.pr_number(found.get("pullRequest"))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/incidents/{ns}/{name}/diff")
def incident_diff(ns: str, name: str, reader: KubeReader = Reader, prs: PRClient = PRs):
    found, number = _incident_pr(ns, name, reader)
    return {"pullRequest": found["pullRequest"], "number": number, "files": prs.diff(number)}


@app.post("/api/incidents/{ns}/{name}/approve")
def approve_incident(
    ns: str, name: str, body: dict = JsonBody, reader: KubeReader = Reader, prs: PRClient = PRs
):
    found, number = _incident_pr(ns, name, reader)
    if found.get("phase") != "AwaitingReview":
        raise HTTPException(409, f"incident is in phase {found.get('phase')!r}; approval needs AwaitingReview")
    if body.get("confirm") is not True:
        raise HTTPException(400, 'explicit confirmation required: send {"confirm": true} after reviewing the diff')
    who = actions.approver()
    sha = prs.merge_squash(number)
    result = {"merged": True, "sha": sha, "approvedBy": who, "recorded": True}
    try:
        reader.set_approved_by(ns, name, who)
    except KubeUnavailable as exc:
        result.update(recorded=False, warning=f"PR merged but approvedBy was not recorded: {exc}")
    return result


@app.post("/api/incidents/{ns}/{name}/request-changes")
def request_changes(
    ns: str, name: str, body: dict = JsonBody, reader: KubeReader = Reader, prs: PRClient = PRs
):
    text = " ".join(str(body.get("text", "")).split())
    if not text:
        raise HTTPException(400, "describe the changes you want")
    if len(text) > 2000:
        raise HTTPException(400, "text is too long (max 2000 characters)")
    _found, number = _incident_pr(ns, name, reader)
    prs.comment(number, f"/repair retry {text}")
    return {"commented": True, "number": number}


def _state_dir() -> Path:
    return Path(os.environ.get("REPAIR_STATE_DIR") or "~/.repair-loop").expanduser()


PROVIDERS = ("groq", "claude-cli")


def _agent_config() -> dict:
    try:
        return json.loads((_state_dir() / "agent-config.json").read_text())
    except (OSError, ValueError):
        return {}


@app.get("/api/agent/config")
def agent_config_get():
    cfg = _agent_config()
    available = {"groq": bool(os.environ.get("GROQ_API_KEY")), "claude-cli": shutil.which("claude") is not None}
    provider = cfg.get("provider") or os.environ.get("REPAIR_AGENT_PROVIDER") or ("groq" if available["groq"] else "claude-cli")
    return {"provider": provider, "model": cfg.get("model") or "", "available": available}


@app.post("/api/agent/config")
def agent_config_set(body: dict = JsonBody):
    provider = body.get("provider")
    if provider not in PROVIDERS:
        raise HTTPException(400, f"provider must be one of {', '.join(PROVIDERS)}")
    cfg = {"provider": provider}
    if body.get("model"):
        cfg["model"] = str(body["model"])[:100]
    d = _state_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent-config.json").write_text(json.dumps(cfg))
    return agent_config_get()


@app.get("/api/incidents/{ns}/{name}/agent-trace")
def agent_trace(ns: str, name: str):
    """Saved agent traces of an incident (one per attempt), latest attempt first."""
    base = _state_dir() / "agent-traces"
    pattern = re.compile(re.escape(name) + r"-a(\d+)\.json$")
    found = []
    for p in base.glob(f"{name}-a*.json") if base.is_dir() else []:
        m = pattern.match(p.name)
        if m:
            try:
                found.append((int(m.group(1)), json.loads(p.read_text())))
            except (OSError, ValueError):
                continue
    return [t for _, t in sorted(found, key=lambda x: -x[0])]


@app.post("/api/pipelines/rerun")
def rerun_pipeline(reader: KubeReader = Reader):
    return {
        "name": reader.submit_workflow(RERUN_NAMESPACE, RELEASE_TEMPLATE),
        "namespace": RERUN_NAMESPACE,
        "template": RELEASE_TEMPLATE,
    }


@app.post("/api/services/{ns}/{name}/restart")
def restart_service_pods(ns: str, name: str, reader: KubeReader = Reader):
    """Restart a service's pods (delete them; the ReplicaSet recreates them).

    Needed after a ConfigMap-only change such as F4: running pods keep the old environment until they restart.
    """
    if ns not in DEFAULT_NAMESPACES:
        raise HTTPException(404, f"namespace {ns!r} is not managed by the demo")
    if not any(s["namespace"] == ns and s["name"] == name for s in reader.services([ns])):
        raise HTTPException(404, f"service {ns}/{name} not found")
    return {"namespace": ns, "name": name, "restarted": reader.restart_pods(ns, name)}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
