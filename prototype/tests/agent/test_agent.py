"""Agent package tests: no network, no real MCP servers, no claude binary."""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from repair_agent import claude_host, config, runner
from repair_agent.groq_host import compact, run_loop
from repair_agent.result import AgentResult


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for k in ("REPAIR_AGENT_PROVIDER", "GROQ_API_KEY", "REPAIR_AGENT_FALLBACK", "REPAIR_AGENT_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("REPAIR_STATE_DIR", str(tmp_path / "state"))


# provider selection

def test_provider_default_depends_on_groq_key(monkeypatch):
    assert config.select_provider() == "claude-cli"
    monkeypatch.setenv("GROQ_API_KEY", "x")
    assert config.select_provider() == "groq"


def test_provider_order(monkeypatch, tmp_path):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("REPAIR_AGENT_PROVIDER", "claude-cli")
    assert config.select_provider() == "claude-cli"  # env beats default
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "agent-config.json").write_text(json.dumps({"provider": "groq", "model": "m1"}))
    assert config.select_provider() == "groq"  # file beats env
    assert config.model_for("groq") == "m1"
    assert config.select_provider("claude-cli") == "claude-cli"  # explicit beats file
    assert config.model_for("claude-cli") == "sonnet"


# runner

def _fake_host(status, calls, name):
    def host(seed, feedback, *, max_steps, timeout_s):
        calls.append(name)
        return AgentResult(status, {"status": status} if status != "failed" else None, usage={"provider": name},
                           error="boom" if status == "failed" else "")
    return host


def test_fallback_runs_other_provider_once(monkeypatch):
    calls = []
    hosts = {"groq": _fake_host("failed", calls, "groq"), "claude-cli": _fake_host("proposed", calls, "claude-cli")}
    monkeypatch.setattr(runner, "_host", hosts.get)
    res = runner.investigate({}, provider="groq")
    assert calls == ["groq", "claude-cli"]
    assert res.status == "proposed" and res.usage["fallback_from"] == "groq"


def test_fallback_disabled_and_both_fail(monkeypatch):
    calls = []
    hosts = {"groq": _fake_host("failed", calls, "groq"), "claude-cli": _fake_host("failed", calls, "claude-cli")}
    monkeypatch.setattr(runner, "_host", hosts.get)
    monkeypatch.setenv("REPAIR_AGENT_FALLBACK", "0")
    assert runner.investigate({}, provider="groq").status == "failed"
    assert calls == ["groq"]
    monkeypatch.delenv("REPAIR_AGENT_FALLBACK")
    res = runner.investigate({}, provider="groq")
    assert res.status == "failed" and "groq" in res.error and "claude-cli" in res.error


def test_investigate_never_raises(monkeypatch):
    def boom(seed, feedback, *, max_steps, timeout_s):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(runner, "_host", lambda p: boom)
    res = runner.investigate({}, provider="groq")
    assert res.status == "failed" and "kaboom" in res.error


def test_files_read_from_trace(monkeypatch):
    def host(seed, feedback, *, max_steps, timeout_s):
        return AgentResult("proposed", {}, trace=[
            {"tool": "git_read", "args": {"path": "deploy/a.yaml"}, "result_preview": "x: 1"},
            {"tool": "git_read", "args": {"path": "deploy/bad.yaml"}, "result_preview": '{"error": "nope"}'},
        ])
    monkeypatch.setattr(runner, "_host", lambda p: host)
    assert runner.investigate({}, provider="groq").files_read == ["deploy/a.yaml"]


# groq loop

class FakeBox:
    def __init__(self, out_path):
        self.out, self.calls = out_path, []
        self.tools = [{"type": "function", "function": {"name": "k8s_logs", "description": "", "parameters": {}}}]

    async def call(self, name, args):
        self.calls.append((name, args))
        if name == "propose_patch":
            Path(self.out).write_text(json.dumps({"status": "proposed", **args}))
            return "recorded; stop", False
        return "log line", False


def _resp(content=None, calls=()):
    tcs = [NS(id=f"c{i}", function=NS(name=n, arguments=a)) for i, (n, a) in enumerate(calls)]
    return NS(choices=[NS(message=NS(content=content, tool_calls=tcs or None))], usage=NS(prompt_tokens=10, completion_tokens=5))


_n = itertools.count()


def _loop(script, tmp_path, **kw):
    box = FakeBox(str(tmp_path / f"out{next(_n)}.json"))
    it = iter(script)
    seen = []

    def create(**kwargs):
        seen.append(len(kwargs["messages"]))
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    res = asyncio.run(run_loop(create, box, system="s", user="u", out_path=box.out, model="m", sleep=lambda s: None, **kw))
    return res, box, seen


def test_groq_loop_tool_then_propose(tmp_path):
    patch = {"file": "deploy/a.yaml", "edits": [{"find": "a", "replace": "b"}]}
    res, _box, _ = _loop([
        _resp(calls=[("k8s_logs", '{"pod": "p"}')]),
        _resp(calls=[("propose_patch", json.dumps(patch))]),
    ], tmp_path)
    assert res.status == "proposed" and res.proposal["file"] == "deploy/a.yaml"
    assert [t["tool"] for t in res.trace] == ["k8s_logs", "propose_patch"]
    assert res.evidence_text.startswith("log line") and res.usage["tool_calls"] == 2


def test_groq_loop_nudge_then_finish_and_give_up(tmp_path):
    res, _, seen = _loop([_resp("I think it is fine"), _resp(calls=[("propose_patch", '{"file": "f"}')])], tmp_path)
    assert res.status == "proposed" and seen[1] == seen[0] + 2  # assistant text + nudge appended
    res, _, _ = _loop([_resp("a"), _resp("b"), _resp("c")], tmp_path)
    assert res.status == "failed" and "without" in res.error


def test_groq_loop_retries_api_errors_and_bad_args(tmp_path):
    res, box, _ = _loop([RuntimeError("503"), _resp(calls=[("k8s_logs", "not json")]),
                         _resp(calls=[("propose_patch", '{"file": "f"}')])], tmp_path)
    assert res.status == "proposed"
    assert box.calls == [("propose_patch", {"file": "f"})]  # bad-args call is not executed
    res, _, _ = _loop([RuntimeError("x")] * 4, tmp_path)
    assert res.status == "failed" and "groq api error" in res.error


def test_groq_loop_budget(tmp_path):
    script = [_resp(calls=[("k8s_logs", json.dumps({"n": i}))]) for i in range(20)]
    res, _, _ = _loop(script, tmp_path, max_steps=3)
    assert res.status == "failed" and res.usage["tool_calls"] == 3


def test_compact_shrinks_old_tool_results():
    msgs = [{"role": "tool", "tool_call_id": str(i), "content": "x" * 5000} for i in range(4)]
    compact(msgs, [], budget=1000)
    assert len(msgs[0]["content"]) < 400 and len(msgs[-1]["content"]) == 5000


# claude stream parsing

SAMPLE = "\n".join(json.dumps(e) for e in [
    {"type": "system", "subtype": "init"},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "mcp__git__git_read", "input": {"path": "deploy/a.yaml"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "image: x:1"}]}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t2", "name": "mcp__repair__escalate", "input": {"reason": "r"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "recorded", "is_error": True}]}},
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "total_cost_usd": 0.01,
     "usage": {"input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 20}},
])


def test_claude_stream_parse(tmp_path):
    p = claude_host.StreamParser()
    for line in SAMPLE.splitlines() + ["garbage{"]:
        p.feed(line)
    assert [t["tool"] for t in p.trace] == ["git_read", "escalate"]
    assert p.trace[0]["result_preview"] == "image: x:1" and p.trace[1].get("error")
    out = tmp_path / "out.json"
    out.write_text(json.dumps({"status": "escalated", "reason": "r"}))
    res = claude_host.build_result(p, str(out), "sonnet", 0.0)
    assert res.status == "escalated" and res.files_read == ["deploy/a.yaml"]
    assert res.usage["input_tokens"] == 150 and res.usage["steps"] == 3 and res.usage["cost_usd"] == 0.01
    assert "image: x:1" in res.evidence_text
    # no outcome file -> failed
    assert claude_host.build_result(p, str(tmp_path / "none.json"), "sonnet", 0.0).status == "failed"


def test_claude_command_is_restricted():
    cmd = claude_host.build_command("claude", "/tmp/c.json", "sonnet")
    assert "--strict-mcp-config" in cmd and cmd[cmd.index("--tools") + 1] == ""
    assert {"mcp__k8s", "mcp__git", "mcp__argo", "mcp__repair"} <= set(cmd)
    assert "ANTHROPIC_API_KEY" not in claude_host.child_env()


def test_groq_retry_hint_parsing():
    from repair_agent.groq_host import hinted_wait, retry_wait

    e = RuntimeError("Please try again in 2m43.296s. Need more")
    assert round(hinted_wait(e)) == 164 and retry_wait(e, 1) == 75.0
    assert hinted_wait(RuntimeError("try again in 250ms")) < 2
    assert hinted_wait(RuntimeError("nothing")) is None
