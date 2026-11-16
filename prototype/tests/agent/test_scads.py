"""ScaDS provider: key loading, rate limits, no-fallback, and the bench skip logic. No network."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from repair_agent import config, runner
from repair_agent.groq_host import run_loop
from repair_agent.result import AgentResult
from repair_agent.scads_host import ScadsHTTPError, make_create

SECRET = "sk-test-secret-123"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for k in ("SCADS_API_KEY", "REPAIR_AGENT_PROVIDER", "REPAIR_AGENT_FALLBACK", "REPAIR_SCADS_MODEL_HIGH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("REPAIR_STATE_DIR", str(tmp_path))


def test_key_from_file_and_env_precedence(monkeypatch, tmp_path):
    assert config.scads_api_key() is None
    (tmp_path / "scads.env").write_text(f"# c\nSCADS_API_KEY={SECRET}\n")
    assert config.scads_api_key() == SECRET
    monkeypatch.setenv("SCADS_API_KEY", "from-env")
    assert config.scads_api_key() == "from-env"


def test_providers_and_models(monkeypatch):
    assert config.select_provider("scads-high") == "scads-high"
    assert config.select_provider("scads:foo/bar") == "scads:foo/bar"
    assert config.model_for("scads:foo/bar") == "foo/bar"
    assert config.model_for("scads-low") == "google/gemma-4-26B-A4B-it"
    monkeypatch.setenv("REPAIR_SCADS_MODEL_HIGH", "x/y")
    assert config.model_for("scads-high") == "x/y"


def test_key_only_in_header_and_never_in_error(caplog):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers["authorization"]
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi", "reasoning_content": "r"}}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 2}})

    create = make_create(SECRET, client=httpx.Client(transport=httpx.MockTransport(handler)))
    resp = create(model="m", messages=[])
    assert resp.choices[0].message.content == "hi" and resp.usage.prompt_tokens == 3
    assert seen["auth"] == f"Bearer {SECRET}" and SECRET not in seen["body"]
    bad = make_create(SECRET, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))))
    with pytest.raises(ScadsHTTPError) as ei:
        bad(model="m", messages=[])
    assert SECRET not in str(ei.value) and SECRET not in caplog.text


class _Box:
    tools: ClassVar[list[dict]] = []

    async def call(self, name, args):  # pragma: no cover
        return "{}", False


def _rate_error(retry_after: str | None):
    headers = {"retry-after": retry_after} if retry_after else {}
    return ScadsHTTPError(httpx.Response(429, headers=headers, text="rate limit exceeded"))


def _loop(create, tmp_path, sleeps):
    async def fake_sleep(s):
        sleeps.append(s)

    import asyncio

    real = asyncio.sleep
    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        return asyncio.run(run_loop(create, _Box(), system="s", user="u", out_path=str(tmp_path / "o.json"), model="m",
                                    provider="scads-low", strict_rate_limit=True, free=True))
    finally:
        asyncio.sleep = real  # type: ignore[assignment]


def test_429_long_retry_after_is_rate_limited(tmp_path):
    sleeps: list[float] = []

    def create(**kw):
        raise _rate_error("120")

    res = _loop(create, tmp_path, sleeps)
    assert res.status == "rate_limited" and "rate limit" in res.error and sleeps == []
    assert res.usage["cost_usd"] == 0 and res.usage["provider"] == "scads-low"


def test_429_short_retry_after_retries_once(tmp_path):
    sleeps: list[float] = []
    calls = []

    def create(**kw):
        calls.append(1)
        raise _rate_error("5")

    res = _loop(create, tmp_path, sleeps)
    assert res.status == "rate_limited" and len(calls) == 2 and sleeps == [5.0]


def test_429_without_header_is_rate_limited(tmp_path):
    def create(**kw):
        raise _rate_error(None)

    assert _loop(create, tmp_path, []).status == "rate_limited"


def test_rate_limited_does_not_fall_back_when_disabled(monkeypatch):
    calls = []

    def host(name, status):
        def h(seed, feedback, *, max_steps, timeout_s):
            calls.append(name)
            return AgentResult(status, usage={"provider": name}, error="rate limit")
        return h

    hosts = {"scads-high": host("scads-high", "rate_limited"), "claude-cli": host("claude-cli", "proposed")}
    monkeypatch.setattr(runner, "_host", hosts.get)
    res = runner.investigate({}, provider="scads-high", fallback=False)
    assert res.status == "rate_limited" and calls == ["scads-high"]
    res = runner.investigate({}, provider="scads-high")
    assert res.status == "proposed" and res.usage["fallback_from"] == "scads-high"


def test_missing_key_fails_cleanly(monkeypatch):
    from repair_agent.scads_host import run_scads

    res = run_scads({}, None, max_steps=1, timeout_s=5, provider="scads-medium")
    assert res.status == "failed" and "SCADS_API_KEY" in res.error


# bench

def _bench():
    spec = importlib.util.spec_from_file_location("model_bench", Path(__file__).parents[2] / "scripts" / "model_bench.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bench_skips_rate_limited_providers():
    b = _bench()
    stopped: dict[str, str] = {}
    b.update_stopped(stopped, {"scads-high": {"status": "rate_limited", "error": "quota"}, "scads-low": {"status": "proposed"}}, "F1")
    assert list(stopped) == ["scads-high"] and "F1" in stopped["scads-high"]
    assert b.active_providers(["claude-cli", "scads-high", "scads-low"], stopped) == ["claude-cli", "scads-low"]


def test_bench_failing_pod_detection(tmp_path):
    b = _bench()

    def pod(name, **status):
        return {"metadata": {"name": name}, "status": status}

    ok = pod("a", phase="Running", containerStatuses=[{"ready": True, "restartCount": 0, "state": {"running": {}}}])
    crash = pod("b", phase="Running", containerStatuses=[{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}])
    assert b.failing_pod([ok]) is None and b.failing_pod([ok, crash]) == ("b", "CrashLoopBackOff")
    pend = pod("c", phase="Pending", conditions=[{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}])
    assert b.failing_pod([pend]) == ("c", "FailedScheduling")
    assert b.pods_healthy([ok]) and not b.pods_healthy([ok, crash])
    f = tmp_path / "x.json"
    f.write_text(json.dumps({"status": "escalated", "proposal": {"reason": "nothing"}, "usage": {"tool_calls": 2, "latency_ms": 9}}))
    assert b.summarize(f)["tool_calls"] == 2 and b.summarize(tmp_path / "none.json")["status"] == "no_result"


def test_failing_pod_ignores_pre_injection_restarts():
    from datetime import datetime

    b = _bench()
    since = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)

    def pod(name, created, **cs):
        return {"metadata": {"name": name, "creationTimestamp": created}, "status": {"phase": "Running", "containerStatuses": [cs]}}

    old = {"reason": "Error", "finishedAt": "2026-11-16T10:00:00Z"}
    new = {"reason": "Error", "finishedAt": "2026-11-16T12:01:00Z"}
    frontend = pod("frontend-aaa", "2026-11-16T09:00:00Z", restartCount=3, state={"running": {}}, lastState={"terminated": old})
    api = pod("api-bbb", "2026-11-16T09:00:00Z", restartCount=1, state={"waiting": {"reason": "CrashLoopBackOff"}}, lastState={"terminated": new})
    assert b.failing_pod([frontend, api], since) == ("api-bbb", "CrashLoopBackOff")
    assert b.failing_pod([frontend], since) is None
    api2 = pod("api-ccc", "2026-11-16T12:00:30Z", state={"waiting": {"reason": "ImagePullBackOff"}})
    assert b.failing_pod([frontend, api2], since, hint="api") == ("api-ccc", "ImagePullBackOff")
    other = pod("worker-x", "2026-11-16T12:00:30Z", state={"waiting": {"reason": "CrashLoopBackOff"}})
    assert b.failing_pod([other, api], since, hint="api")[0] == "api-bbb"
    assert b.workload_hint({"target_file": "deploy/demo/worker.yaml"}) == "worker"
    assert b.workload_hint({"target_file": "deploy/demo/configmaps.yaml", "restart_target": "api"}) == "api"


def test_new_default_levels():
    assert config.model_for("scads-high") == "mistralai/Mistral-Large-4"
    assert config.model_for("scads-medium") == "meta-llama/Llama-3.3-70B-Instruct"


def test_pacing_interval_and_env(monkeypatch):
    from repair_agent import scads_host as sh

    assert sh.min_interval_s("scads-low") == 1.5
    monkeypatch.setenv("REPAIR_SCADS_MIN_INTERVAL_S", "2")
    monkeypatch.setenv("REPAIR_SCADS_MIN_INTERVAL_LOW", "0.5")
    assert sh.min_interval_s("scads-high") == 2.0 and sh.min_interval_s("scads-low") == 0.5
    sh._next_slot.clear()
    waits: list[float] = []
    t = [100.0]
    for _ in range(3):
        sh.pace("m", 1.5, clock=lambda: t[0], sleep=waits.append)
    assert waits == [1.5, 3.0]


def _503():
    return ScadsHTTPError(httpx.Response(503, text="temporarily unavailable, likely due to a temporary restart"))


def test_503_backs_off_then_unavailable(tmp_path):
    sleeps: list[float] = []
    calls = []

    def create(**kw):
        calls.append(1)
        raise _503()

    res = _loop(create, tmp_path, sleeps)
    assert res.status == "unavailable" and sleeps == [10, 20, 40, 60] and len(calls) == 5


def test_503_recovers(tmp_path):
    sleeps: list[float] = []
    n = []

    def create(**kw):
        n.append(1)
        if len(n) == 1:
            raise _503()
        raise _rate_error(None)

    assert _loop(create, tmp_path, sleeps).status == "rate_limited" and sleeps == [10]


def test_no_tool_call_loop_guard(tmp_path):
    from types import SimpleNamespace as NS

    def create(**kw):
        return NS(choices=[NS(message=NS(content="thinking", tool_calls=None))], usage=None)

    res = _loop(create, tmp_path, [])
    assert res.status == "failed" and res.error == "model stopped calling tools"


def test_bench_records_unavailable_not_stopped():
    b = _bench()
    stopped: dict[str, str] = {}
    b.update_stopped(stopped, {"scads-high": {"status": "unavailable"}}, "F4")
    assert stopped == {}
