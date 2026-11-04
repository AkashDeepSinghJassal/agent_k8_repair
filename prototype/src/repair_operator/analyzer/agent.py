"""Approach C adapter: evidence bundle -> seed -> ``repair_agent.runner.investigate`` -> Proposal."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ..config import get_settings
from ..models import Edit, Proposal
from ..retrigger import app_name

log = logging.getLogger(__name__)
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def build_seed(bundle: dict) -> dict:
    src = bundle.get("source") or {}
    failure = bundle.get("failure") or {}
    wf = bundle.get("workflow") or {}
    kind = "Workflow" if src.get("kind") == "Workflow" else "Pod"
    ns = src.get("namespace") or ""
    return {
        "incident": bundle.get("incident"),
        "namespace": ns,
        "kind": kind,
        "name": src.get("name"),
        "owner": src.get("owner"),
        "container": src.get("container"),
        "reason": failure.get("reason") or wf.get("phase") or "",
        "classHint": "",
        "message": (failure.get("message") or wf.get("message") or "")[:500],
        "repo": get_settings().demo_repo,
        "app": app_name(ns, workflow=kind == "Workflow"),
        "path": (bundle.get("git") or {}).get("path"),
    }


def save_trace(bundle: dict, attempt: int, seed: dict, result: object) -> None:
    try:
        base = Path(get_settings().state_dir).expanduser() / "agent-traces"
        base.mkdir(parents=True, exist_ok=True)
        data = {
            "incident": bundle.get("incident"), "attempt": attempt, "seed": seed,
            "status": getattr(result, "status", None), "error": getattr(result, "error", ""),
            "proposal": getattr(result, "proposal", None), "usage": getattr(result, "usage", {}),
            "files_read": getattr(result, "files_read", []), "trace": getattr(result, "trace", []),
        }
        name = _SAFE.sub("_", str(bundle.get("incident") or "unknown"))
        (base / f"{name}-a{attempt}.json").write_text(json.dumps(data, indent=2, default=str))
    except Exception:
        log.exception("could not save agent trace")


def _flow(incident: str, ns: str, kind: str, **kw: object) -> str | None:
    try:
        from .. import flow

        return flow.emit(incident, ns, kind, **kw)  # type: ignore[arg-type]
    except Exception:
        log.debug("flow emit failed", exc_info=True)
        return None


def _tool_sink(incident: str, ns: str, parent: str | None):
    """on_event callback for ``runner.investigate``: one ``tool_call`` flow event per completed tool call."""
    def sink(ev: dict) -> None:
        _flow(incident, ns, "tool_call", title=f"Agent called {ev.get('tool')}", tier="T3", parent=parent,
              status="error" if ev.get("error") else "ok", duration_ms=ev.get("ms"),
              data={k: ev.get(k) for k in ("server", "tool", "args", "result", "ms", "error", "step")})
    return sink


def _agent_end(incident: str, ns: str, start_id: str | None, result: object) -> None:
    try:
        u = getattr(result, "usage", None) or {}
        status = getattr(result, "status", "")
        data = {"status": status, "error": getattr(result, "error", ""),
                **{k: u.get(k) for k in ("provider", "model", "steps", "input_tokens", "output_tokens", "cost_usd",
                                         "latency_ms", "fallback_from")}}
        _flow(incident, ns, "agent_end", title=f"Agent {status}" + (f" after {u.get('steps')} steps" if u.get("steps") else ""),
              tier="T3", parent=start_id, status="error" if status == "failed" else "ok", data=data,
              duration_ms=u.get("latency_ms"))
    except Exception:
        log.debug("on_tool callback failed", exc_info=True)


def _escalated(cls: str, root_cause: str, rationale: str, confidence: float = 1.0) -> Proposal:
    return Proposal(
        **{"class": cls}, root_cause=root_cause, confidence=confidence, fixable_in_git=False,
        rationale=rationale, tier=4, source="agent",
    )


def agent_analyze(bundle: dict, feedback: list[str] | None = None, **_: object) -> Proposal:
    from repair_agent import runner  # lazy: only needed in agent mode

    seed = build_seed(bundle)
    attempt = int(bundle.get("attempt") or 1)
    inc, ns = str(bundle.get("incident") or ""), seed.get("namespace") or ""
    start_id = _flow(inc, ns, "agent_start", title="Agent started", tier="T3", data={"seed": seed, "attempt": attempt})
    on_event = _tool_sink(inc, ns, start_id)
    result = runner.investigate(seed, list(feedback or []) or None, on_event=on_event)
    save_trace(bundle, attempt, seed, result)
    _agent_end(inc, ns, start_id, result)

    p = result.proposal or {}
    if result.status == "proposed" and p.get("edits"):
        prop = Proposal(
            **{"class": p.get("failure_class") or "OTHER"},
            root_cause=p.get("root_cause", ""),
            confidence=float(p.get("confidence", 0.0)),
            fixable_in_git=True,
            file=p.get("file"),
            edits=[Edit(find=e["find"], replace=e["replace"]) for e in p["edits"]],
            rationale=p.get("rationale", ""),
            verify=p.get("verify", ""),
            tier=3,
            source="agent",
        )
    elif result.status == "escalated":
        prop = _escalated(p.get("failure_class") or "OTHER", p.get("root_cause") or p.get("reason", ""), p.get("reason", ""))
    else:
        reason = result.error or ("agent proposed nothing" if result.status == "proposed" else "agent failed")
        prop = _escalated("OTHER", "agent investigation failed", reason, confidence=0.0)
    prop.agent_meta = {**(result.usage or {}), "status": result.status, "error": result.error, "trace": result.trace}
    prop.agent_evidence = result.evidence_text or ""
    prop.agent_files = list(result.files_read or [])
    return prop
