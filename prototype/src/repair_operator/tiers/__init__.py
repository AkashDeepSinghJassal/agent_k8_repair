"""Tier executors (V4_CONTRACT.md section 3): T1 deterministic replay, T2 replay + one narrow LLM call, router."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .. import flow
from ..crystallize import apply_rule, seed_vars, substitute
from ..playbooks import Playbook


class TierError(Exception):
    """A tier could not produce an edit (tool failure, extraction failure, invalid LLM output)."""


@dataclass
class TierResult:
    tier: str
    ok: bool
    error: str = ""
    file: str = ""
    edits: list[dict] = field(default_factory=list)
    root_cause: str = ""
    confidence: float = 0.0
    rationale: str = ""
    evidence: str = ""
    files: list[str] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


class Tracer:
    """Emits flow events for one incident and tier; never raises."""

    def __init__(self, incident: str = "", namespace: str = "", tier: str | None = None, parent: str | None = None):
        self.incident, self.namespace, self.tier, self.parent = incident, namespace, tier, parent

    def emit(self, kind: str, title: str, data: dict | None = None, status: str = "ok", **kw: Any) -> str | None:
        if not self.incident:
            return None
        return flow.emit(self.incident, self.namespace, kind, title=title, data=data, tier=self.tier,
                         parent=self.parent, status=status, **kw)


@dataclass
class Replay:
    outputs: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)  # [{tool, args, result}]
    ctx: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    tool_failures: int = 0

    def file_text(self, file: str) -> str | None:
        for s in self.steps:
            if s["tool"] == "git_read" and s["args"].get("path") == file and not s.get("error"):
                return s["result"]
        return None


async def replay(pb: Playbook, seed: dict, mcp: Any, tracer: Tracer, *, strict: bool) -> Replay:
    """Run the playbook's tool calls in order and bind the extract variables. ``strict`` raises on the first failure."""
    out = Replay(ctx=seed_vars(seed))
    for i, step in enumerate(pb.steps):
        try:
            args = substitute(step.args, out.ctx)
        except KeyError as exc:
            msg = f"step {i + 1} ({step.tool}): variable {exc.args[0]} is not bound"
            tracer.emit("tool_call", f"Cannot replay {step.tool}: {msg}", {"server": step.server, "tool": step.tool, "step": i + 1}, "error")
            if strict:
                raise TierError(msg) from exc
            out.errors.append(msg)
            out.tool_failures += 1
            out.outputs.append("")
            out.steps.append({"tool": step.tool, "args": step.args, "result": "", "error": True})
            continue
        t0 = time.monotonic()
        text, is_err = await mcp.call(step.tool, args)
        ms = int((time.monotonic() - t0) * 1000)
        tracer.emit("tool_call", f"{step.server}.{step.tool} (replay)", {
            "server": step.server, "tool": step.tool, "args": args, "result": text, "ms": ms, "error": is_err, "step": i + 1,
        }, "error" if is_err else "ok", duration_ms=ms)
        out.outputs.append(text)
        out.steps.append({"tool": step.tool, "args": args, "result": text, "error": is_err})
        if is_err:
            msg = f"step {i + 1} ({step.tool}) returned an error: {text[:160]}"
            if strict:
                raise TierError(msg)
            out.errors.append(msg)
            out.tool_failures += 1
            continue
        for ex in (e for e in pb.extracts if e.from_step == i):
            value = apply_rule(ex.rule, text)
            ok = value is not None
            tracer.emit("extract", f"Extracted {ex.var} from step {i + 1} ({step.tool})" if ok else
                        f"Could not extract {ex.var} from step {i + 1} ({step.tool})",
                        {"variable": ex.var, "from_step": i + 1, "rule": ex.rule, **({"value": value} if ok else {"error": "rule did not match"})},
                        "ok" if ok else "error")
            if ok:
                out.ctx[ex.var] = value  # type: ignore[assignment]
            else:
                msg = f"extract {ex.var} did not match the output of step {i + 1} ({step.tool})"
                if strict:
                    raise TierError(msg)
                out.errors.append(msg)
    return out


def run_sync(coro: Any) -> Any:
    """Run a coroutine from sync code, also when an event loop is already running in this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict = {}

    def target() -> None:
        try:
            box["v"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            box["e"] = exc

    t = threading.Thread(target=target)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["v"]


def read_files(rep: Replay) -> list[str]:
    return [s["args"]["path"] for s in rep.steps if s["tool"] == "git_read" and not s.get("error") and s["args"].get("path")]


from . import router, t1, t2

__all__ = ["Replay", "TierError", "TierResult", "Tracer", "replay", "router", "run_sync", "t1", "t2"]
