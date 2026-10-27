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


def _escalated(cls: str, root_cause: str, rationale: str, confidence: float = 1.0) -> Proposal:
    return Proposal(
        **{"class": cls}, root_cause=root_cause, confidence=confidence, fixable_in_git=False,
        rationale=rationale, tier=4, source="agent",
    )


def agent_analyze(bundle: dict, feedback: list[str] | None = None, **_: object) -> Proposal:
    from repair_agent import runner  # lazy: only needed in agent mode

    seed = build_seed(bundle)
    attempt = int(bundle.get("attempt") or 1)
    result = runner.investigate(seed, list(feedback or []) or None)
    save_trace(bundle, attempt, seed, result)

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
