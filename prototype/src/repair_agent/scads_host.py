"""ScaDS.AI LLM service (TU Dresden): OpenAI-compatible chat completions, driven by the shared tool loop."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx

from .config import SCADS_BASE_URL, model_for, scads_api_key
from .groq_host import run_groq
from .result import AgentResult

SCADS_CTX_TOKENS = 100_000  # large-context models: no need for Groq-style compaction


class ScadsHTTPError(Exception):
    """HTTP error from the service. ``status_code`` and ``response`` mirror the SDK exceptions the loop inspects."""

    def __init__(self, response: httpx.Response):
        self.status_code = response.status_code
        self.response = response
        super().__init__(f"HTTP {response.status_code}: {response.text[:300]}")


def _ns(obj: Any) -> Any:
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_ns(v) for v in obj]
    return obj


def make_create(api_key: str, base_url: str = SCADS_BASE_URL, timeout: float = 300, client: httpx.Client | None = None):
    """``client.chat.completions.create`` look-alike. The key lives only in the Authorization header."""
    http = client or httpx.Client(timeout=timeout)

    def create(**kw: Any) -> Any:
        r = http.post(f"{base_url.rstrip('/')}/chat/completions", json=kw,
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        if r.status_code >= 400:
            raise ScadsHTTPError(r)
        try:
            return _ns(r.json())
        except (ValueError, json.JSONDecodeError) as e:
            raise ScadsHTTPError(r) from e

    return create


def run_scads(seed: dict, feedback: list[str] | None, *, max_steps: int, timeout_s: int, provider: str = "scads-medium",
              on_tool: Any = None) -> AgentResult:
    model = model_for(provider)
    key = scads_api_key()

    def build():
        if not key:
            raise RuntimeError("SCADS_API_KEY not set (env or ~/.repair-loop/scads.env)")
        return make_create(key)

    return run_groq(
        seed, feedback, max_steps=max_steps, timeout_s=timeout_s, model=model, on_tool=on_tool, provider=provider,
        make_create=build,
        loop_kwargs={"ctx_tokens": SCADS_CTX_TOKENS, "strict_rate_limit": True, "max_tokens_key": "max_tokens", "free": True},
    )
