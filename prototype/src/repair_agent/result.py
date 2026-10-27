"""AgentResult shared by both hosts and the runner."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

PREVIEW = 500


@dataclass
class AgentResult:
    status: str  # "proposed" | "escalated" | "failed"
    proposal: dict | None = None
    trace: list[dict] = field(default_factory=list)
    evidence_text: str = ""
    files_read: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    error: str = ""


def read_outcome(path: str | Path) -> dict | None:
    """The proposal/escalation the repair server wrote, or None."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("status") in ("proposed", "escalated") else None


def preview(text: str) -> str:
    return text if len(text) <= PREVIEW else text[:PREVIEW] + "..."


def short_tool(name: str) -> str:
    """mcp__git__git_read -> git_read; k8s__k8s_logs -> k8s_logs."""
    if name.startswith("mcp__"):
        return name.split("__", 2)[-1]
    return name.split("__", 1)[-1]


def files_from_trace(trace: list[dict]) -> list[str]:
    out: list[str] = []
    for t in trace:
        if short_tool(t.get("tool", "")) == "git_read" and not t.get("error"):
            p = (t.get("args") or {}).get("path")
            if p and p not in out and not str(t.get("result_preview", "")).startswith('{"error"'):
                out.append(p)
    return out
