"""ScaDS.AI LLM service (TU Dresden): OpenAI-compatible chat completions, driven by the shared tool loop."""

from __future__ import annotations

import json
import os
import threading
import time
from types import SimpleNamespace
from typing import Any

import httpx

from .config import SCADS_BASE_URL, model_for, scads_api_key
from .groq_host import run_groq
from .result import AgentResult

SCADS_CTX_TOKENS = 100_000  # large-context models: no need for Groq-style compaction


_pace_lock = threading.Lock()
_next_slot: dict[str, float] = {}


def min_interval_s(provider: str) -> float:
    """Minimum seconds between model calls: REPAIR_SCADS_MIN_INTERVAL_<LEVEL> else REPAIR_SCADS_MIN_INTERVAL_S (1.5)."""
    for name in (f"REPAIR_SCADS_MIN_INTERVAL_{provider.removeprefix('scads-').upper()}", "REPAIR_SCADS_MIN_INTERVAL_S"):
        if provider.startswith("scads-") or name == "REPAIR_SCADS_MIN_INTERVAL_S":
            try:
                return max(0.0, float(os.environ[name]))
            except (KeyError, ValueError):
                continue
    return 1.5


def pace(model: str, interval: float, *, clock=time.monotonic, sleep=time.sleep) -> None:
    """Block until ``interval`` has passed since the previous call for ``model`` in this process (keeps under 60 req/min)."""
    with _pace_lock:
        now = clock()
        slot = max(now, _next_slot.get(model, 0.0))
        _next_slot[model] = slot + interval
    if slot > now:
        sleep(slot - now)


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


def make_create(api_key: str, base_url: str = SCADS_BASE_URL, timeout: float = 300, client: httpx.Client | None = None,
                interval: float = 0.0):
    """``client.chat.completions.create`` look-alike. The key lives only in the Authorization header."""
    http = client or httpx.Client(timeout=timeout)

    def create(**kw: Any) -> Any:
        if interval > 0:
            pace(str(kw.get("model")), interval)
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
        return make_create(key, interval=min_interval_s(provider))

    return run_groq(
        seed, feedback, max_steps=max_steps, timeout_s=timeout_s, model=model, on_tool=on_tool, provider=provider,
        make_create=build,
        loop_kwargs={"ctx_tokens": SCADS_CTX_TOKENS, "strict_rate_limit": True, "max_tokens_key": "max_tokens", "free": True},
    )
