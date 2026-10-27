"""Output-channel MCP server ("repair"). Its only side effect is writing JSON to $REPAIR_AGENT_OUT."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import common as c

DONE = "recorded; your investigation is complete, stop now"


def _write(payload: dict, out: str | None = None) -> None:
    path = out or os.environ.get("REPAIR_AGENT_OUT")
    if not path:
        raise c.ToolError("REPAIR_AGENT_OUT not set")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(p)


def _failure_class(value: str) -> str:
    if value and value not in c.FAILURE_CLASSES:
        raise c.ToolError(f"failure_class must be one of {c.FAILURE_CLASSES}")
    return value


@c.tool
def propose_patch(
    file: str,
    edits: list[dict],
    root_cause: str,
    failure_class: str,
    confidence: float,
    rationale: str,
    verify: str = "",
    repo: str = "",
    *,
    out: str | None = None,
) -> Any:
    c.check_path(file)
    if not edits:
        raise c.ToolError("edits must not be empty")
    for i, e in enumerate(edits):
        if not isinstance(e, dict) or not isinstance(e.get("find"), str) or not isinstance(e.get("replace"), str):
            raise c.ToolError(f"edit {i} needs string fields find and replace")
        if not e["find"]:
            raise c.ToolError(f"edit {i}: find must not be empty")
        if e["find"] == e["replace"]:
            raise c.ToolError(f"edit {i}: find equals replace")
    if not failure_class:
        raise c.ToolError("failure_class is required")
    _failure_class(failure_class)
    if not 0 <= float(confidence) <= 1:
        raise c.ToolError("confidence must be between 0 and 1")
    _write({
        "status": "proposed",
        "file": file,
        "edits": [{"find": e["find"], "replace": e["replace"]} for e in edits],
        "root_cause": root_cause,
        "failure_class": failure_class,
        "confidence": float(confidence),
        "rationale": rationale,
        "verify": verify,
        "repo": repo,
    }, out)
    return DONE


@c.tool
def escalate(reason: str, root_cause: str = "", failure_class: str = "", *, out: str | None = None) -> Any:
    _failure_class(failure_class)
    _write({"status": "escalated", "reason": reason, "root_cause": root_cause, "failure_class": failure_class}, out)
    return DONE


def build_server() -> MCPServer:
    srv = MCPServer("repair")

    @srv.tool(
        name="propose_patch",
        description=(
            "FINISH with a fix: exact text edits to one deploy/** or workflows/** yaml file you read with git_read. "
            "Each edit is {find, replace}; `find` must appear verbatim in the file. failure_class: "
            + ", ".join(c.FAILURE_CLASSES) + ". confidence 0..1. Call once, then stop."
        ),
    )
    def propose_patch_(
        file: str,
        edits: list[dict],
        root_cause: str,
        failure_class: str,
        confidence: float,
        rationale: str,
        verify: str = "",
        repo: str = "",
    ) -> Any:
        return propose_patch(file, edits, root_cause, failure_class, confidence, rationale, verify, repo)

    @srv.tool(
        name="escalate",
        description="FINISH without a fix when a safe config patch is not possible (needs a human). Give reason and, if known, root_cause. Call once, then stop.",
    )
    def escalate_(reason: str, root_cause: str = "", failure_class: str = "") -> Any:
        return escalate(reason, root_cause, failure_class)

    return srv


if __name__ == "__main__":
    build_server().run()
