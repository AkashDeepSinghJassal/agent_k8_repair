"""investigate(): provider selection, host run, one-shot fallback to the other provider. Never raises."""

from __future__ import annotations

import time

from .config import fallback_enabled, other_provider, select_provider
from .result import AgentResult, files_from_trace

__all__ = ["AgentResult", "investigate"]


def _host(provider: str):
    if provider == "claude-cli":
        from .claude_host import run_claude

        return run_claude
    from .groq_host import run_groq

    return run_groq


def _run(provider: str, seed: dict, feedback, max_steps: int, timeout_s: int) -> AgentResult:
    try:
        res = _host(provider)(seed, feedback, max_steps=max_steps, timeout_s=timeout_s)
    except Exception as e:  # noqa: BLE001
        res = AgentResult("failed", usage={"provider": provider}, error=f"{type(e).__name__}: {str(e)[:300]}")
    res.usage.setdefault("provider", provider)
    if not res.files_read:
        res.files_read = files_from_trace(res.trace)
    return res


def investigate(seed: dict, feedback: list[str] | None = None, *, provider: str | None = None,
                max_steps: int = 30, timeout_s: int = 600) -> AgentResult:
    started = time.monotonic()
    try:
        first = select_provider(provider)
        res = _run(first, seed, feedback, max_steps, timeout_s)
        if res.status == "failed" and fallback_enabled():
            second = other_provider(first)
            alt = _run(second, seed, feedback, max_steps, timeout_s)
            alt.usage["fallback_from"] = first
            if alt.status == "failed":
                alt.error = f"{first}: {res.error}; {second}: {alt.error}"
            res = alt
        res.usage["latency_ms"] = int((time.monotonic() - started) * 1000)
        return res
    except Exception as e:  # last-resort guard  # noqa: BLE001
        return AgentResult("failed", usage={"provider": provider or ""}, error=f"{type(e).__name__}: {str(e)[:300]}")
