"""T1: deterministic replay. Tool calls through MCP, extract rules, render the edit. No LLM."""

from __future__ import annotations

from typing import Any

from .. import crystallize
from ..playbooks import Playbook
from . import TierError, TierResult, Tracer, read_files, replay


async def run(playbook: Playbook, seed: dict, mcp: Any, tracer: Tracer | None = None) -> TierResult:
    tracer = tracer or Tracer()
    if playbook.t1_blocked:
        raise TierError(f"T1 is blocked for this playbook: {playbook.t1_blocked}")
    rep = await replay(playbook, seed, mcp, tracer, strict=True)
    try:
        file, edits = crystallize.render(playbook, rep.ctx)
    except KeyError as exc:
        raise TierError(f"edit template needs variable {exc.args[0]}, which no step produced") from exc
    text = rep.file_text(file)
    if text is not None:
        for e in edits:
            if text.count(e["find"]) != 1:
                raise TierError(f"edit anchor {e['find'][:60]!r} is not found exactly once in {file}")
    return TierResult(
        "T1", True, file=file, edits=edits, root_cause=playbook.meta.get("root_cause", ""), confidence=0.95,
        rationale=f"Rendered by deterministic playbook {playbook.id} (replayed {len(playbook.steps)} tool calls, no LLM).",
        evidence="\n".join(rep.outputs), files=read_files(rep),
        trace=[{"step": i + 1, "tool": s["tool"], "args": s["args"], "result_preview": s["result"][:500]} for i, s in enumerate(rep.steps)],
        meta={"provider": "playbook", "model": playbook.id, "steps": len(rep.steps)},
    )
