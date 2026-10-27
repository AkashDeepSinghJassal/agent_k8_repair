"""Own agent loop: Groq chat completions with tool calling, tools executed through MCP."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol

from .config import model_for
from .mcp_tools import McpToolbox
from .prompt import NUDGE, SYSTEM_PROMPT, render_seed
from .result import AgentResult, files_from_trace, preview, read_outcome
from .servers import server_specs

MAX_NUDGES = 2
API_RETRIES = 6  # mostly TPM rate limits on small Groq tiers; each wait follows the server's hint
MODEL_RESULT_CHARS = 6000
CTX_TOKENS = int(os.environ.get("REPAIR_AGENT_CTX_TOKENS", "5500"))  # Groq free tier allows only 8000 TPM per request
OLD_RESULT_CHARS = 200


def _est_tokens(messages: list[dict], tools: list[dict]) -> int:
    return (len(json.dumps(messages, default=str)) + len(json.dumps(tools))) // 3


def compact(messages: list[dict], tools: list[dict], budget: int, keep_recent: int = 2) -> None:
    """Shrink old tool results in place until the estimate fits ``budget`` tokens.

    Oldest first; git_read results (needed to copy exact ``find`` text) are shrunk only after everything else.
    """
    names = {c["id"]: c["function"]["name"] for m in messages for c in m.get("tool_calls") or []}
    idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    old = idx[:-keep_recent] if keep_recent else idx
    order = [i for i in old if names.get(messages[i]["tool_call_id"]) != "git_read"]
    order += [i for i in old if names.get(messages[i]["tool_call_id"]) == "git_read"]
    for i in order:
        if _est_tokens(messages, tools) <= budget:
            return
        c = messages[i]["content"]
        if len(c) > OLD_RESULT_CHARS:
            messages[i]["content"] = c[:OLD_RESULT_CHARS] + " ...[older result shortened; call the tool again if needed]"


def hinted_wait(exc: Exception) -> float | None:
    """The server's 'try again in 1m2.5s' hint in seconds, uncapped."""
    m = re.search(r"try again in (?:(\d+)m(?!s))?(?:(\d+(?:\.\d+)?)(ms|s))?", str(exc))
    if m and (m.group(1) or m.group(2)):
        secs = int(m.group(1) or 0) * 60 + (float(m.group(2)) / (1000 if m.group(3) == "ms" else 1) if m.group(2) else 0)
        return secs + 1
    return None


def retry_wait(exc: Exception, default: float) -> float:
    """Seconds to wait before retrying: the server hint (or retry-after header), capped."""
    if (hint := hinted_wait(exc)) is not None:
        return min(max(hint, 1.0), 75.0)
    try:
        return min(max(float(exc.response.headers["retry-after"]), 1.0), 75.0)  # type: ignore[attr-defined]
    except (AttributeError, KeyError, TypeError, ValueError):
        return min(default, 30.0)


class ToolCaller(Protocol):
    tools: list[dict]

    async def call(self, name: str, args: dict) -> tuple[str, bool]: ...


def _make_groq():
    import groq

    return groq.Groq(max_retries=0, timeout=120).chat.completions.create


async def run_loop(
    create: Any,
    toolbox: ToolCaller,
    *,
    system: str,
    user: str,
    out_path: str,
    model: str,
    max_steps: int = 30,
    timeout_s: float = 600,
    sleep: Any = time.sleep,
) -> AgentResult:
    """``create`` is ``client.chat.completions.create``. Returns an AgentResult (never raises)."""
    started = time.monotonic()
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    trace: list[dict] = []
    evidence: list[str] = []
    usage = {"provider": "groq", "model": model, "steps": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0}
    nudges = 0
    seen_calls: dict[str, int] = {}

    def done(status: str, error: str = "") -> AgentResult:
        usage["latency_ms"] = int((time.monotonic() - started) * 1000)
        outcome = read_outcome(out_path)
        if outcome is not None:
            status, error = outcome["status"], ""
        return AgentResult(status, outcome, trace, "\n".join(evidence), files_from_trace(trace), usage, error)

    async def llm(kw: dict) -> Any:
        last: Exception | None = None
        budget = CTX_TOKENS
        for attempt in range(API_RETRIES + 1):
            compact(messages, toolbox.tools, budget)
            try:
                return await asyncio.to_thread(lambda: create(**kw))
            except Exception as e:  # noqa: BLE001
                last = e
                if getattr(e, "status_code", None) == 413:
                    budget = int(budget * 0.7)
                    compact(messages, toolbox.tools, budget, keep_recent=0)
                hint = hinted_wait(e)
                if hint is not None and hint > 90:  # daily quota or long outage: let the runner fall back
                    break
                if attempt < API_RETRIES and time.monotonic() - started < timeout_s:
                    wait = retry_wait(e, 2.0 * 2**attempt)
                    if os.environ.get("REPAIR_AGENT_DEBUG"):
                        print(f"[groq] api error {type(e).__name__} {str(e)[:120]}; wait {wait:.0f}s", file=sys.stderr, flush=True)
                    await asyncio.to_thread(sleep, wait)
        raise last  # type: ignore[misc]

    try:
        while True:
            if time.monotonic() - started > timeout_s:
                return done("failed", "timeout")
            kw: dict[str, Any] = {
                "model": model, "messages": messages, "tools": toolbox.tools, "tool_choice": "auto",
                "max_completion_tokens": 8192,
            }
            if model.startswith("openai/gpt-oss"):
                kw["reasoning_effort"] = os.environ.get("REPAIR_AGENT_REASONING_EFFORT", "medium")
            try:
                resp = await llm(kw)
            except Exception as e:  # noqa: BLE001
                return done("failed", f"groq api error: {type(e).__name__}: {str(e)[:300]}")
            usage["steps"] += 1
            if os.environ.get("REPAIR_AGENT_DEBUG"):
                print(f"[groq] step {usage['steps']} at {time.monotonic() - started:.0f}s", file=sys.stderr, flush=True)
            u = getattr(resp, "usage", None)
            if u is not None:
                usage["input_tokens"] += getattr(u, "prompt_tokens", 0) or 0
                usage["output_tokens"] += getattr(u, "completion_tokens", 0) or 0
            msg = resp.choices[0].message
            calls = list(getattr(msg, "tool_calls", None) or [])
            if not calls:
                if read_outcome(out_path) is not None:
                    return done("failed")
                if nudges >= MAX_NUDGES:
                    return done("failed", "model stopped without propose_patch/escalate")
                nudges += 1
                messages.append({"role": "assistant", "content": getattr(msg, "content", None) or ""})
                messages.append({"role": "user", "content": NUDGE})
                continue
            messages.append({
                "role": "assistant", "content": getattr(msg, "content", None) or "",
                "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                    for c in calls
                ],
            })
            for c in calls:
                name = c.function.name
                try:
                    args = json.loads(c.function.arguments or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be an object")  # noqa: TRY004
                except ValueError as e:
                    text, is_err = json.dumps({"error": f"invalid tool arguments: {e}"}), True
                    args = {}
                else:
                    key = name + json.dumps(args, sort_keys=True)
                    seen_calls[key] = seen_calls.get(key, 0) + 1
                    if seen_calls[key] > 2:
                        text, is_err = json.dumps({"error": "you already made this identical call twice; stop re-reading and call propose_patch or escalate now"}), True
                    elif usage["tool_calls"] >= max_steps:
                        text, is_err = json.dumps({"error": "tool budget exhausted; call propose_patch or escalate now"}), True
                    else:
                        t0 = time.monotonic()
                        text, is_err = await toolbox.call(name, args)
                        ms = int((time.monotonic() - t0) * 1000)
                        usage["tool_calls"] += 1
                        entry = {"step": len(trace) + 1, "tool": name, "args": args, "result_preview": preview(text), "ms": ms}
                        if is_err:
                            entry["error"] = True
                        trace.append(entry)
                        evidence.append(text)
                shown = text if len(text) <= MODEL_RESULT_CHARS else text[:MODEL_RESULT_CHARS] + "\n[truncated]"
                messages.append({"role": "tool", "tool_call_id": c.id, "content": shown})
            if read_outcome(out_path) is not None:
                return done("failed")
            if usage["tool_calls"] >= max_steps + 3:
                return done("failed", "tool budget exhausted")
    except Exception as e:  # noqa: BLE001
        return done("failed", f"{type(e).__name__}: {str(e)[:300]}")


def run_groq(seed: dict, feedback: list[str] | None, *, max_steps: int, timeout_s: int, model: str | None = None) -> AgentResult:
    model = model or model_for("groq")
    if not os.environ.get("GROQ_API_KEY"):
        return AgentResult("failed", usage={"provider": "groq", "model": model}, error="GROQ_API_KEY not set")

    async def main() -> AgentResult:
        with tempfile.TemporaryDirectory(prefix="repair-agent-") as tmp:
            out = str(Path(tmp) / "out.json")
            async with McpToolbox(server_specs(out)) as box:
                return await run_loop(
                    _make_groq(), box, system=SYSTEM_PROMPT, user=render_seed(seed, feedback), out_path=out,
                    model=model, max_steps=max_steps, timeout_s=timeout_s,
                )

    try:
        return asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        return AgentResult("failed", usage={"provider": "groq", "model": model}, error=f"{type(e).__name__}: {str(e)[:300]}")
