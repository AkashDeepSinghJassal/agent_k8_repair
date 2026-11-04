"""Flow trace events emitted by the operator and the agent hosts (V4_CONTRACT.md section 1)."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from repair_agent import claude_host, runner
from repair_operator import flow, guardrails, incidents, repair_flow
from repair_operator.analyzer import agent as agent_mod
from repair_operator.models import Edit, Phase, Proposal

NAME, NS = "ri-flow", "demo"
BODY = {"metadata": {"name": NAME, "namespace": NS}}
PATH = "deploy/demo/api.yaml"
BUNDLE = {
    "incident": NAME, "attempt": 1, "git": {"path": PATH},
    "source": {"kind": "Pod", "name": "api-1", "namespace": NS, "owner": "Deployment/api", "container": "api"},
    "failure": {"reason": "CrashLoopBackOff", "message": "boom"},
}


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    s = SimpleNamespace(state_dir=tmp_path, demo_repo="o/r")
    for mod in (flow, agent_mod):
        monkeypatch.setattr(mod, "get_settings", lambda s=s: s)
    return s


def kinds():
    return [e["kind"] for e in flow.read(NAME, NS)]


def test_agent_analyze_emits_tool_calls_under_agent_start(monkeypatch):
    def investigate(seed, feedback=None, on_event=None, **_):
        on_event({"server": "git", "tool": "git_diff", "args": {"path": PATH}, "result": "FULL OUTPUT", "ms": 12,
                  "error": False, "step": 1})
        on_event({"server": "k8s", "tool": "k8s_logs", "args": {"pod": "api-1"}, "result": '{"error": "x"}', "ms": 3,
                  "error": True, "step": 2})
        return SimpleNamespace(status="failed", proposal=None, error="nope", evidence_text="", files_read=[],
                               usage={"provider": "claude-cli", "model": "m", "steps": 2, "latency_ms": 20}, trace=[])

    monkeypatch.setitem(sys.modules, "repair_agent", types.ModuleType("repair_agent"))
    mod = types.ModuleType("repair_agent.runner")
    mod.investigate = investigate
    monkeypatch.setitem(sys.modules, "repair_agent.runner", mod)
    monkeypatch.setattr(sys.modules["repair_agent"], "runner", mod, raising=False)

    agent_mod.agent_analyze(BUNDLE)
    ev = flow.read(NAME, NS)
    assert [e["kind"] for e in ev] == ["agent_start", "tool_call", "tool_call", "agent_end"]
    start, t1, t2, end = ev
    assert t1["parent"] == start["id"] == end["parent"] and t1["tier"] == "T3"
    assert t1["title"] == "Agent called git_diff"
    assert t1["data"]["args"] == {"path": PATH} and t1["data"]["result"] == "FULL OUTPUT" and t1["data"]["server"] == "git"
    assert t2["status"] == "error" and end["status"] == "error" and end["data"]["provider"] == "claude-cli"


def test_runner_on_event_passes_through_and_survives_a_failing_callback(monkeypatch):
    def host(seed, feedback, *, max_steps, timeout_s, on_tool=None):
        on_tool({"tool": "t", "step": 1})
        return runner.AgentResult("escalated", {"status": "escalated"}, usage={"provider": "claude-cli"})

    monkeypatch.setattr(runner, "_host", lambda provider: host)
    got = []
    assert runner.investigate({}, on_event=got.append, provider="claude-cli").status == "escalated"
    assert got == [{"tool": "t", "step": 1}]

    def boom(_):
        raise RuntimeError("callback bug")

    assert runner.investigate({}, on_event=boom, provider="claude-cli").status == "escalated"


def test_claude_stream_parser_emits_on_tool_result_with_full_text():
    got = []
    p = claude_host.StreamParser(got.append)
    big = "x" * 25_000
    p.feed(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "u1", "name": "mcp__git__git_read", "input": {"path": PATH}}]}}))
    assert got == []
    p.feed(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "u1", "content": big}]}}))
    assert len(got) == 1 and got[0]["server"] == "git" and got[0]["tool"] == "git_read"
    assert got[0]["args"] == {"path": PATH} and got[0]["step"] == 1 and got[0]["error"] is False
    assert len(got[0]["result"]) < len(big) and got[0]["result"].startswith("xxx")
    assert p.trace[0]["result"] == got[0]["result"] and len(p.trace[0]["result_preview"]) < 600


def test_guardrail_events_one_per_check():
    prop = Proposal(**{"class": "CONFIG_ERROR"}, root_cause="r", confidence=0.9, fixable_in_git=True, file=PATH,
                    edits=[Edit(find="a: 1", replace="a: 2")], tier=3, source="agent")
    text = "a: 1\n"
    res = guardrails.check(prop, text, "a: 2\n", PATH, skip=("static", "server_dry_run", "policy"),
                           allowed_paths=[PATH], evidence_text="a: 2")
    assert [c["name"] for c in res.checks] == ["path_allowlist", "exact_edit", "size_cap", "grounded_values"]
    repair_flow._flow_guardrails(BODY, prop, res)
    ev = [e for e in flow.read(NAME, NS) if e["kind"] == "guardrail"]
    assert ev[0]["title"] == "Guardrail path_allowlist passed" and len(ev) == 4
    assert all(e["status"] == "ok" and e["tier"] == "T3" for e in ev)

    prop.edits = [Edit(find="a: 1", replace="a: https://never-seen.example.invalid/x")]
    bad = guardrails.check(prop, text, "a: https://never-seen.example.invalid/x\n", PATH, skip=("static", "server_dry_run", "policy"),
                           allowed_paths=[PATH], evidence_text="nothing relevant")
    assert not bad.ok and bad.checks[-1]["name"] == "grounded_values" and bad.checks[-1]["ok"] is False


def test_terminal_phase_emits_outcome_and_non_terminal_does_not():
    patch: dict = {}
    incidents.set_phase(patch, Phase.ANALYZING, incident=BODY)
    assert kinds() == []
    incidents.set_phase(patch, Phase.ESCALATED, incident=BODY, outcome={"result": "escalated", "reason": "sync_timeout"})
    (ev,) = flow.read(NAME, NS)
    assert ev["kind"] == "outcome" and ev["status"] == "error"
    assert ev["data"]["phase"] == "Escalated" and ev["data"]["reason"] == "sync_timeout"
    incidents.set_phase({}, Phase.HEALED, incident=BODY, outcome={"result": "healed", "reason": "stable"})
    assert flow.read(NAME, NS)[-1]["status"] == "ok"


def test_span_start_and_end_merge_in_read():
    with flow.span(NAME, NS, "evidence", title="Collecting", data={"a": 1}) as (eid, extra):
        assert flow.read(NAME, NS)[0]["status"] == "running"
        extra["b"] = 2
    (ev,) = flow.read(NAME, NS)
    assert ev["id"] == eid and ev["status"] == "ok" and ev["data"] == {"a": 1, "b": 2} and "duration_ms" in ev
    assert len(Path(flow.path_for(NAME, NS)).read_text().splitlines()) == 2


def test_emit_for_never_raises(monkeypatch):
    monkeypatch.setattr(flow, "emit", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert flow.emit_for(BODY, "detected", title="x") is None
    assert flow.emit_for(None, "detected", title="x") is None
