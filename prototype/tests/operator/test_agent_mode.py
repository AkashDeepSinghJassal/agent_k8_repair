"""Agent analyzer mode with a fake ``repair_agent.runner.investigate``."""

import json
import sys
import types
from types import SimpleNamespace

import pytest
from test_pr_flow import PATH, SKIP, make_env

from repair_operator import analyzer
from repair_operator.analyzer import agent as agent_mod
from repair_operator.config import Settings
from repair_operator.models import Proposal

BUNDLE = {
    "incident": "ri-x", "attempt": 2, "git": {"path": PATH},
    "source": {"kind": "Pod", "name": "api-1", "namespace": "demo", "owner": "Deployment/api", "container": "api"},
    "failure": {"reason": "CrashLoopBackOff", "message": "boom"},
}


def fake_runner(monkeypatch, result, calls=None):
    def investigate(seed, feedback=None, **kw):
        if calls is not None:
            calls.append((seed, feedback))
        return result

    monkeypatch.setitem(sys.modules, "repair_agent", types.ModuleType("repair_agent"))
    mod = types.ModuleType("repair_agent.runner")
    mod.investigate = investigate
    monkeypatch.setitem(sys.modules, "repair_agent.runner", mod)
    monkeypatch.setattr(sys.modules["repair_agent"], "runner", mod, raising=False)


def result(status, proposal=None, error=""):
    return SimpleNamespace(
        status=status, proposal=proposal, error=error, evidence_text="SECRET-VALUE-42", files_read=[PATH],
        usage={"provider": "groq", "model": "m", "steps": 2},
        trace=[{"step": 1, "tool": "git_read", "args": {}, "result_preview": "x"}, {"step": 2, "tool": "propose_patch", "args": {}, "result_preview": "ok"}],
    )


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = Settings(state_dir=tmp_path, analyzer_mode="agent")
    monkeypatch.setattr(agent_mod, "get_settings", lambda: s)
    return s


PROPOSED = {"status": "proposed", "file": PATH, "edits": [{"find": "a", "replace": "b"}], "root_cause": "rc",
            "failure_class": "CONFIG_ERROR", "confidence": 0.8, "rationale": "why", "verify": "ready"}


def test_proposed_to_tier3(monkeypatch, settings):
    calls = []
    fake_runner(monkeypatch, result("proposed", PROPOSED), calls)
    p = analyzer.analyze(BUNDLE, ["fb"], settings=settings)
    assert (p.source, p.tier, p.cls, p.file, p.fixable_in_git) == ("agent", 3, "CONFIG_ERROR", PATH, True)
    assert p.edits[0].find == "a" and p.agent_files == [PATH] and p.agent_evidence == "SECRET-VALUE-42"
    assert calls[0][0]["namespace"] == "demo" and calls[0][0]["app"] == "demo-app" and calls[0][1] == ["fb"]
    assert "agent_meta" not in p.model_dump()
    trace = json.loads((settings.state_dir / "agent-traces" / "ri-x-a2.json").read_text())
    assert trace["status"] == "proposed" and len(trace["trace"]) == 2


def test_escalated_and_failed_tier4(monkeypatch, settings):
    fake_runner(monkeypatch, result("escalated", {"status": "escalated", "reason": "needs capacity", "failure_class": "UNSCHEDULABLE"}))
    p = agent_mod.agent_analyze(BUNDLE)
    assert (p.tier, p.fixable_in_git, p.cls, p.rationale) == (4, False, "UNSCHEDULABLE", "needs capacity")
    fake_runner(monkeypatch, result("failed", None, "groq down"))
    p = agent_mod.agent_analyze(BUNDLE)
    assert p.tier == 4 and not p.fixable_in_git and "groq down" in p.rationale


def test_analyze_step_grounds_on_agent_evidence(tmp_path):
    prop = Proposal(
        **{"class": "CONFIG_ERROR"}, root_cause="rc", confidence=0.9, fixable_in_git=True, file=PATH,
        edits=[], rationale="r", tier=3, source="agent", agent_evidence="", agent_files=[PATH],
        agent_meta={"provider": "groq", "model": "m", "trace": [{"tool": "git_read"}, {"tool": "propose_patch"}], "fallback_from": "claude-cli"},
    )
    seen = {}

    def analyze_fn(bundle, feedback=None, **_):
        seen["attempt"] = bundle.get("attempt")
        return prop

    env, cur = make_env(tmp_path, analyze_fn, analyzer_mode="agent")
    prop.edits = [__import__("repair_operator.models", fromlist=["Edit"]).Edit(find=cur, replace="memory: 777Mi")]
    prop.agent_evidence = "limit is 777Mi per quota"
    guard_seen = {}

    def guard(proposal, current, new, path, *, skip=(), allowed_paths=(), evidence_text=""):
        guard_seen["ev"], guard_seen["allowed"] = evidence_text, list(allowed_paths)
        return SimpleNamespace(ok=True, rejected_by=None, details="")

    p: dict = {}
    from repair_operator import repair_flow

    repair_flow.analyze_step(env.body, p, repo=env.repo, analyze_fn=analyze_fn, guard_fn=guard, guard_skip=SKIP,
                             settings=env.settings, collect_fn=env.collect_fn)
    env.apply(p)
    assert env.status["phase"] == "Proposed" and seen["attempt"] == 1
    assert "777Mi" in guard_seen["ev"] and PATH in guard_seen["allowed"]
    assert env.status["agent"] == {"provider": "groq", "model": "m", "steps": 2, "tools": ["git_read", "propose_patch"], "fallbackFrom": "claude-cli"}


def test_agent_failure_escalates_with_reason(tmp_path):
    prop = Proposal(**{"class": "OTHER"}, root_cause="x", confidence=0.0, fixable_in_git=False, rationale="groq down",
                    tier=4, source="agent", agent_meta={"status": "failed", "trace": []})
    env, _ = make_env(tmp_path, lambda b, f=None, **k: prop, analyzer_mode="agent")
    env.analyze()
    assert env.status["phase"] == "Escalated" and env.status["outcome"]["reason"].startswith("agent_failed: groq down")


def test_config_accepts_agent_and_rejects_junk():
    assert Settings.from_env({"REPAIR_ANALYZER_MODE": "agent"}).analyzer_mode == "agent"
    with pytest.raises(ValueError):
        Settings.from_env({"REPAIR_ANALYZER_MODE": "nope"})
