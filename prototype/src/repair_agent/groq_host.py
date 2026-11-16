"""Own agent loop: Groq chat completions with tool calling, tools executed through MCP."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from .config import model_for
from .mcp_tools import McpToolbox
from .prompt import NUDGE, SYSTEM_PROMPT, render_seed
from .result import AgentResult, cap_result, files_from_trace, preview, read_outcome
from .servers import server_specs

log = logging.getLogger(__name__)

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


UNAVAILABLE_BACKOFF_S = (10, 20, 40, 60)  # ~2.5 min in total
MAX_STALLED_STEPS = 3


class Unavailable(Exception):
    """Provider returned 503 / 'temporarily unavailable' for the whole backoff schedule (strict mode)."""


def is_unavailable(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 503 or "temporarily unavailable" in str(exc).lower()


class RateLimited(Exception):
    """Provider quota / rate limit that is not worth waiting for (strict mode)."""


def is_rate_limited(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 429:
        return True
    low = str(exc).lower()
    return "rate limit" in low or "rate_limit" in low or "quota" in low


def retry_after_s(exc: Exception) -> float | None:
    try:
        return float(exc.response.headers["retry-after"])  # type: ignore[attr-defined]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


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
    on_tool: Any = None,
    provider: str = "groq",
    ctx_tokens: int | None = None,
    strict_rate_limit: bool = False,
    max_tokens_key: str = "max_completion_tokens",
    free: bool = False,
) -> AgentResult:
    """``create`` is ``client.chat.completions.create``. Returns an AgentResult (never raises)."""
    started = time.monotonic()
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    trace: list[dict] = []
    evidence: list[str] = []
    usage = {"provider": provider, "model": model, "steps": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0}
    if free:
        usage["cost_usd"] = 0
    retried_rl = False
    nudges = 0
    seen_calls: dict[str, int] = {}
    stalled = 0  # consecutive model replies that executed no tool

    def left() -> float:
        return timeout_s - (time.monotonic() - started)

    timeout_msg = f"timeout after {timeout_s}s"

    def done(status: str, error: str = "") -> AgentResult:
        usage["latency_ms"] = int((time.monotonic() - started) * 1000)
        outcome = read_outcome(out_path)
        if outcome is not None:
            status, error = outcome["status"], ""
        return AgentResult(status, outcome, trace, "\n".join(evidence), files_from_trace(trace), usage, error)

    async def llm(kw: dict) -> Any:
        last: Exception | None = None
        nonlocal retried_rl
        unavailable_tries = 0
        budget = ctx_tokens or CTX_TOKENS
        for attempt in range(API_RETRIES + 1):
            compact(messages, toolbox.tools, budget)
            try:
                return await asyncio.wait_for(asyncio.to_thread(lambda: create(**kw)), timeout=max(0.1, left()))
            except TimeoutError:
                raise
            except Exception as e:
                last = e
                if strict_rate_limit and is_unavailable(e):
                    if unavailable_tries >= len(UNAVAILABLE_BACKOFF_S) or left() <= UNAVAILABLE_BACKOFF_S[unavailable_tries]:
                        raise Unavailable(f"{type(e).__name__}: {str(e)[:300]}") from e
                    wait = UNAVAILABLE_BACKOFF_S[unavailable_tries]
                    unavailable_tries += 1
                    await asyncio.sleep(wait)
                    continue
                if strict_rate_limit and is_rate_limited(e):
                    ra = retry_after_s(e)
                    if retried_rl or ra is None or ra > 20 or left() <= ra:
                        raise RateLimited(f"{type(e).__name__}: {str(e)[:300]}") from e
                    retried_rl = True
                    await asyncio.sleep(ra)
                    continue
                if getattr(e, "status_code", None) == 413:
                    budget = int(budget * 0.7)
                    compact(messages, toolbox.tools, budget, keep_recent=0)
                hint = hinted_wait(e)
                if hint is not None and hint > 90:  # daily quota or long outage: let the runner fall back
                    break
                if attempt < API_RETRIES and left() > 0:
                    wait = min(retry_wait(e, 2.0 * 2**attempt), left())
                    if os.environ.get("REPAIR_AGENT_DEBUG"):
                        print(f"[{provider}] api error {type(e).__name__} {str(e)[:120]}; wait {wait:.0f}s", file=sys.stderr, flush=True)
                    await asyncio.sleep(wait) if sleep is time.sleep else await asyncio.to_thread(sleep, wait)
        raise last  # type: ignore[misc]

    try:
        while True:
            if time.monotonic() - started > timeout_s:
                return done("failed", timeout_msg)
            kw: dict[str, Any] = {
                "model": model, "messages": messages, "tools": toolbox.tools, "tool_choice": "auto",
                max_tokens_key: 8192,
            }
            if model.startswith("openai/gpt-oss"):
                kw["reasoning_effort"] = os.environ.get("REPAIR_AGENT_REASONING_EFFORT", "medium")
            try:
                resp = await llm(kw)
            except TimeoutError:
                return done("failed", timeout_msg)
            except RateLimited as e:
                return done("rate_limited", str(e))
            except Unavailable as e:
                return done("unavailable", str(e))
            except Exception as e:  # noqa: BLE001
                return done("failed", f"{provider} api error: {type(e).__name__}: {str(e)[:300]}")
            usage["steps"] += 1
            if os.environ.get("REPAIR_AGENT_DEBUG"):
                print(f"[{provider}] step {usage['steps']} at {time.monotonic() - started:.0f}s", file=sys.stderr, flush=True)
            u = getattr(resp, "usage", None)
            if u is not None:
                usage["input_tokens"] += getattr(u, "prompt_tokens", 0) or 0
                usage["output_tokens"] += getattr(u, "completion_tokens", 0) or 0
                details = getattr(u, "completion_tokens_details", None)
                rt = getattr(details, "reasoning_tokens", 0) if details is not None else 0
                if rt:  # already part of completion_tokens; reported separately
                    usage["reasoning_tokens"] = usage.get("reasoning_tokens", 0) + rt
            msg = resp.choices[0].message
            calls = list(getattr(msg, "tool_calls", None) or [])
            if not calls:
                if read_outcome(out_path) is not None:
                    return done("failed")
                stalled += 1
                if stalled >= MAX_STALLED_STEPS:
                    return done("failed", "model stopped calling tools")
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
            executed_before = usage["tool_calls"]
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
                        try:
                            text, is_err = await asyncio.wait_for(toolbox.call(name, args), timeout=max(0.1, left()))
                        except TimeoutError:
                            return done("failed", timeout_msg)
                        ms = int((time.monotonic() - t0) * 1000)
                        usage["tool_calls"] += 1
                        server = (getattr(toolbox, "server_names", None) or {}).get(name, "")
                        entry = {"step": len(trace) + 1, "tool": name, "args": args, "result_preview": preview(text),
                                 "result": cap_result(text), "ms": ms, "server": server}
                        if is_err:
                            entry["error"] = True
                        trace.append(entry)
                        if on_tool is not None:
                            try:
                                on_tool({"server": server, "tool": name, "args": args, "result": entry["result"], "ms": ms,
                                         "error": bool(is_err), "step": entry["step"]})
                            except Exception:
                                log.debug("on_tool callback failed", exc_info=True)
                        evidence.append(text)
                shown = text if len(text) <= MODEL_RESULT_CHARS else text[:MODEL_RESULT_CHARS] + "\n[truncated]"
                messages.append({"role": "tool", "tool_call_id": c.id, "content": shown})
            if read_outcome(out_path) is not None:
                return done("failed")
            # replies whose every call was rejected (repeat, bad arguments, budget) make no progress either
            stalled = 0 if usage["tool_calls"] > executed_before else stalled + 1
            if stalled >= MAX_STALLED_STEPS:
                return done("failed", "model stopped calling tools (repeated or invalid calls only)")
            if usage["tool_calls"] >= max_steps + 3:
                return done("failed", "tool budget exhausted")
    except Exception as e:  # noqa: BLE001
        return done("failed", f"{type(e).__name__}: {str(e)[:300]}")


def run_groq(seed: dict, feedback: list[str] | None, *, max_steps: int, timeout_s: int, model: str | None = None,
             on_tool: Any = None, provider: str = "groq", make_create: Any = None, key_error: str = "GROQ_API_KEY not set",
             loop_kwargs: dict | None = None) -> AgentResult:
    """Shared OpenAI-compatible tool loop. ``make_create`` (default: Groq client) builds ``chat.completions.create``."""
    model = model or model_for(provider)
    if make_create is None and not os.environ.get("GROQ_API_KEY"):
        return AgentResult("failed", usage={"provider": provider, "model": model}, error=key_error)
    if make_create is not None:
        try:
            create = make_create()
        except Exception as e:  # noqa: BLE001
            return AgentResult("failed", usage={"provider": provider, "model": model}, error=str(e)[:300])
    else:
        create = None

    async def main() -> AgentResult:
        with tempfile.TemporaryDirectory(prefix="repair-agent-") as tmp:
            out = str(Path(tmp) / "out.json")
            async with McpToolbox(server_specs(out)) as box:
                return await run_loop(
                    create or _make_groq(), box, system=SYSTEM_PROMPT, user=render_seed(seed, feedback), out_path=out,
                    model=model, max_steps=max_steps, timeout_s=timeout_s, on_tool=on_tool, provider=provider,
                    **(loop_kwargs or {}),
                )

    # Hard guard: run in a daemon thread so a wedged HTTP/MCP call cannot outlive the deadline.
    box: list[AgentResult] = []

    def target() -> None:
        try:
            box.append(asyncio.run(main()))
        except Exception as e:  # noqa: BLE001
            box.append(AgentResult("failed", usage={"provider": provider, "model": model}, error=f"{type(e).__name__}: {str(e)[:300]}"))

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout_s + 20)
    if box:
        return box[0]
    return AgentResult("failed", usage={"provider": provider, "model": model}, error=f"timeout after {timeout_s}s")
