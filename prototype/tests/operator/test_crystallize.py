"""Progressive crystallization: capture, extract rules, T1/T2 executors, router fallback, promotion, learn events."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from repair_operator import crystallize, flow, verify_handlers
from repair_operator.analyzer import agent as agent_mod
from repair_operator.config import Settings
from repair_operator.models import Edit, Proposal
from repair_operator.playbooks import PlaybookStore
from repair_operator.tiers import TierError, Tracer, router, t1, t2

NS, FILE = "demo", "deploy/demo/configmaps.yaml"
CM = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: api-config\ndata:\n  LOG_LEVEL: info\n"
DIFF = "commit {sha}\n--- a/{f}\n+++ b/{f}\n@@ -5,4 +5,3 @@\n data:\n   LOG_LEVEL: info\n-  DB_URL: redis://redis:6379\n"
EDIT = {"find": "  LOG_LEVEL: info", "replace": "  LOG_LEVEL: info\n  DB_URL: redis://redis:6379"}
SEED = {"incident": "ri-a", "namespace": NS, "kind": "Pod", "name": "api-7d9-xk2lm", "owner": "Deployment/api",
        "container": "api", "reason": "CrashLoopBackOff", "message": "DB_URL missing", "repo": "o/r", "app": "demo", "path": FILE}
SIG = "abcdef123456"


def f4_trace(pod="api-7d9-xk2lm", sha="abc1234def"):
    log = json.dumps([{"sha": sha, "date": "2026-01-01", "message": "tune config"}])
    return [
        {"step": 1, "tool": "k8s_logs", "args": {"namespace": NS, "pod": pod, "container": "api"}, "result": "KeyError: DB_URL"},
        {"step": 2, "tool": "git_read", "args": {"path": FILE}, "result": CM},
        {"step": 3, "tool": "git_log", "args": {"path": FILE}, "result": log},
        {"step": 4, "tool": "git_diff", "args": {"path": FILE, "sha": sha}, "result": DIFF.format(sha=sha, f=FILE)},
        {"step": 5, "tool": "git_read", "args": {"path": FILE}, "result": CM},  # exact repeat, dropped
        {"step": 6, "tool": "k8s_events", "args": {"namespace": NS}, "result": '{"error": "nope"}', "error": True},
        {"step": 7, "tool": "propose_patch", "args": {}, "result": "ok"},
    ]


class FakeMcp:
    def __init__(self, sha="fff9999", diff_ok=True, pod="api-9z8-qwert"):
        self.calls, self.sha, self.diff_ok, self.pod = [], sha, diff_ok, pod

    async def call(self, name, args):
        self.calls.append((name, args))
        if name == "k8s_logs":
            assert args["pod"] == self.pod
            return "KeyError: DB_URL", False
        if name == "git_read":
            return CM, False
        if name == "git_log":
            return json.dumps([{"sha": self.sha, "date": "d", "message": "m"}]), False
        if name == "git_diff":
            assert args["sha"] == self.sha
            return (DIFF.format(sha=self.sha, f=FILE) if self.diff_ok else "commit x\n(no diff)"), False
        return "{}", False


def captured():
    pb = crystallize.capture(f4_trace(), {"file": FILE, "edits": [EDIT], "failure_class": "CONFIG_ERROR", "root_cause": "rc"}, SEED, signature=SIG)
    assert pb is not None
    return pb


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = Settings(state_dir=tmp_path, analyzer_mode="hybrid", promote_t2_after=2, promote_t1_after=2)
    for mod in (flow, crystallize, agent_mod, verify_handlers):
        monkeypatch.setattr(mod, "get_settings", lambda s=s: s)
    return s


@pytest.fixture
def store(settings):
    return PlaybookStore(settings.state_dir / "playbooks.db")


def test_capture_f4_playbook():
    pb = captured()
    assert [s.tool for s in pb.steps] == ["k8s_logs", "git_read", "git_log", "git_diff"]
    assert pb.steps[0].args == {"namespace": "${namespace}", "pod": "${pod}", "container": "${container}"}
    assert pb.steps[3].args == {"path": "${file}", "sha": "${sha_2}"}
    assert pb.file_template == "${file}"
    rules = {e.var: (e.from_step, e.rule) for e in pb.extracts}
    assert rules["sha_2"] == (2, {"type": "json_path", "path": "[0].sha"})
    assert rules["db_url"] == (3, {"type": "diff_removed_line", "key": "DB_URL"})
    assert pb.edits_template == [{"find": "  LOG_LEVEL: info", "replace": "  LOG_LEVEL: info\n  DB_URL: ${db_url}"}]
    assert pb.t1_blocked is None and pb.state == "T3"
    assert pb.action_sequence == ["k8s_logs", "git_read", "git_log", "git_diff"]


def test_missing_provenance_blocks_t1():
    edit = {"find": "  LOG_LEVEL: info", "replace": "  LOG_LEVEL: info\n  DB_URL: redis://elsewhere:1"}
    pb = crystallize.capture(f4_trace(), {"file": FILE, "edits": [edit], "failure_class": "CONFIG_ERROR"}, SEED, signature=SIG)
    assert "no provenance" in pb.t1_blocked
    with pytest.raises(TierError):
        import asyncio

        asyncio.run(t1.run(pb, {**SEED}, FakeMcp()))


async def test_t1_renders_same_edit_for_new_pod():
    pb = captured()
    seed = {**SEED, "name": "api-9z8-qwert"}
    mcp = FakeMcp()
    res = await t1.run(pb, seed, mcp, Tracer())
    assert res.ok and res.file == FILE and res.edits == [EDIT]
    assert "redis://redis:6379" in res.evidence and res.files == [FILE]
    assert mcp.calls[0] == ("k8s_logs", {"namespace": NS, "pod": "api-9z8-qwert", "container": "api"})


async def test_t1_extract_failure_raises():
    with pytest.raises(TierError, match="db_url"):
        await t1.run(captured(), {**SEED, "name": "api-9z8-qwert"}, FakeMcp(diff_ok=False))


async def test_t2_fake_llm_and_rules_match():
    pb = captured()
    prompts = []

    def llm(prompt):
        prompts.append(prompt)
        return t2.LlmReply(json.dumps({"file": FILE, "edits": [EDIT], "root_cause": "x", "confidence": 0.9}),
                           {"provider": "fake", "model": "m", "input_tokens": 5, "output_tokens": 2})

    res = await t2.run(pb, {**SEED, "name": "api-9z8-qwert"}, FakeMcp(), llm)
    assert res.edits == [EDIT] and res.meta["rules_match"] is True and res.meta["provider"] == "fake"
    assert "<FILL db_url>" in prompts[0] and "redis://redis:6379" in prompts[0]

    def other(prompt):
        e = {**EDIT, "replace": EDIT["replace"].replace("6379", "6380")}
        return t2.LlmReply(json.dumps({"file": FILE, "edits": [e]}))

    assert (await t2.run(pb, {**SEED, "name": "api-9z8-qwert"}, FakeMcp(), other)).meta["rules_match"] is False


def mcp_factory(mcp):
    @asynccontextmanager
    async def cm():
        yield mcp

    return lambda: cm()


def bundle():
    return {"incident": "ri-a", "signature": SIG, "git": {"path": FILE}, "attempt": 1,
            "source": {"kind": "Pod", "name": "api-9z8-qwert", "namespace": NS, "owner": "Deployment/api", "container": "api"},
            "failure": {"reason": "CrashLoopBackOff", "message": "m"}}


def t3_stub(calls):
    def t3(b, fb=None):
        calls.append(fb)
        return Proposal(**{"class": "CONFIG_ERROR"}, root_cause="agent", confidence=0.8, fixable_in_git=True, file=FILE,
                        edits=[Edit(**EDIT)], tier=3, source="agent")

    return t3


def routes():
    return [e for e in flow.read("ri-a", NS) if e["kind"] == "route"]


def test_router_t1_proposal_is_grounded(settings, store):
    pb = captured()
    pb.state, pb.runs["t2_match"] = "T1", 2
    store.upsert(pb)
    prop = router.analyze_hybrid(bundle(), None, settings=settings, store=store, mcp_factory=mcp_factory(FakeMcp()), t3=t3_stub([]))
    assert (prop.tier_used, prop.playbook_id, prop.source, prop.file) == ("T1", pb.id, "agent", FILE)
    assert prop.edits[0].replace == EDIT["replace"] and "redis://redis:6379" in prop.agent_evidence and prop.agent_files == [FILE]
    assert "replaying without LLM" in routes()[0]["data"]["reason"] and routes()[0]["data"]["chosen_tier"] == "T1"


def test_router_falls_back_t1_t2_t3(settings, store):
    pb = captured()
    pb.state = "T1"
    store.upsert(pb)
    t3calls = []

    def bad_llm(prompt):
        return t2.LlmReply("not json")

    prop = router.analyze_hybrid(bundle(), None, settings=settings, store=store,
                                 mcp_factory=mcp_factory(FakeMcp(diff_ok=False)), llm=bad_llm, t3=t3_stub(t3calls))
    assert prop.tier_used == "T3" and len(t3calls) == 1
    r = routes()
    assert [(x["data"]["chosen_tier"], x["data"]["fallback_from"]) for x in r] == [("T1", None), ("T2", "T1"), ("T3", "T2")]
    assert store.get(pb.id).state == "T3"  # demoted twice
    learn = [e["data"]["action"] for e in flow.read("ri-a", NS) if e["kind"] == "learn"]
    assert learn == ["demoted", "demoted"]


def test_router_t2_fallback_succeeds_and_feedback_skips_t1(settings, store):
    pb = captured()
    pb.state = "T1"
    store.upsert(pb)
    llm = lambda p: t2.LlmReply(json.dumps({"file": FILE, "edits": [EDIT]}))
    prop = router.analyze_hybrid(bundle(), None, settings=settings, store=store, mcp_factory=mcp_factory(FakeMcp(diff_ok=False)), llm=llm, t3=t3_stub([]))
    assert prop.tier_used == "T2" and store.get(pb.id).state == "T2"
    # a retry with feedback: previous attempt ran on T2 and was rejected -> demoted to T3 -> agent
    calls = []
    prop = router.analyze_hybrid(bundle(), ["reviewer says no"], settings=settings, store=store, mcp_factory=mcp_factory(FakeMcp()), llm=llm, t3=t3_stub(calls))
    assert prop.tier_used == "T3" and calls == [["reviewer says no"]] and store.get(pb.id).state == "T3"


def test_router_no_playbook_runs_t3(settings, store):
    calls = []
    prop = router.analyze_hybrid(bundle(), None, settings=settings, store=store, t3=t3_stub(calls))
    assert prop.tier_used == "T3" and prop.playbook_id is None and routes()[0]["data"]["playbook_id"] is None


# --- learning hook ----------------------------------------------------------------------------------


def healed_body(name, tier="T3", pb_id=None, attempts=1, rules_match=None, phase="Healed"):
    return {"metadata": {"name": name, "namespace": NS}, "status": {
        "phase": phase, "signature": SIG, "mergeSha": "sha1", "attempts": attempts,
        "proposal": {"file": FILE, "edits": [EDIT], "class": "CONFIG_ERROR"},
        "agent": {"tierUsed": tier, "playbookId": pb_id, "rulesMatch": rules_match}}}


def save_trace(settings, name, trace=None):
    d = settings.state_dir / "agent-traces"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}-a1.json").write_text(json.dumps({"seed": SEED, "trace": trace or f4_trace()}))


def learn(settings, store, body, merged=None):
    patch = {}
    merged = merged if merged is not None else CM.replace("  LOG_LEVEL: info", EDIT["replace"])
    act = crystallize.learn_from_incident(body, patch, object(), store=store, settings=settings, fetch=lambda *a: merged)
    return act, patch


def learn_actions(name):
    return [e["data"]["action"] for e in flow.read(name, NS) if e["kind"] == "learn"]


def test_capture_observe_promote_and_demote(settings, store):
    save_trace(settings, "ri-1")
    save_trace(settings, "ri-2")
    act, patch = learn(settings, store, healed_body("ri-1"))
    pb = store.get_by_signature(SIG)
    assert act == "captured" and pb.state == "T3" and pb.runs["t3_ok"] == 1 and patch["status"]["hybridLearning"]["action"] == "captured"
    assert learn(settings, store, healed_body("ri-2", pb_id=pb.id))[0] == "observed"
    pb = store.get(pb.id)
    assert pb.state == "T2" and pb.consistency == 1.0 and learn_actions("ri-2") == ["observed", "promoted"]
    for n in ("ri-3", "ri-4"):
        assert learn(settings, store, healed_body(n, "T2", pb.id, rules_match=True))[0] == "observed"
    pb = store.get(pb.id)
    assert pb.state == "T1" and pb.runs["t2_ok"] == 2 and pb.runs["t2_match"] == 2
    assert learn_actions("ri-4") == ["observed", "promoted"]
    act, _ = learn(settings, store, healed_body("ri-5", "T1", pb.id, phase="Failed"))
    assert act == "demoted" and store.get(pb.id).state == "T2" and learn_actions("ri-5") == ["demoted"]


def test_t2_without_rule_match_does_not_promote(settings, store):
    pb = captured()
    pb.state, pb.runs["t3_ok"] = "T2", 2
    store.upsert(pb)
    for n in ("ri-6", "ri-7"):
        learn(settings, store, healed_body(n, "T2", pb.id, rules_match=False))
    assert store.get(pb.id).state == "T2"


def test_reviewer_edit_and_unclean_runs_are_skipped(settings, store):
    save_trace(settings, "ri-1")
    act, _ = learn(settings, store, healed_body("ri-1"), merged=CM)  # reviewer changed the fix
    assert act == "skipped" and store.get_by_signature(SIG) is None and learn_actions("ri-1") == ["skipped"]
    assert "reviewer edited" in next(e for e in flow.read("ri-1", NS) if e["kind"] == "learn")["data"]["reason"]
    act, _ = learn(settings, store, healed_body("ri-8", attempts=2))
    assert act == "skipped"
    pb = captured()
    pb.state = "T2"
    store.upsert(pb)
    act, _ = learn(settings, store, healed_body("ri-9", "T2", pb.id), merged=CM)
    assert act == "demoted" and store.get(pb.id).state == "T3" and store.get(pb.id).runs["edited"] == 1


def test_closed_step_runs_learner_only_in_hybrid(settings, store, monkeypatch):
    save_trace(settings, "ri-1")
    monkeypatch.setattr(crystallize, "default_store", lambda s=None: store)
    monkeypatch.setattr(verify_handlers, "fetch_merged_file", lambda *a: CM.replace("  LOG_LEVEL: info", EDIT["replace"]))
    patch = {}
    verify_handlers.closed_step(healed_body("ri-1"), patch, repo=object())
    assert patch["status"]["hybridLearning"]["action"] == "captured"
    assert store.get_by_signature(SIG) is not None


def _f4_fixture():
    from pathlib import Path

    return json.loads((Path(__file__).parent / "fixtures" / "f4_real_trace.json").read_text())


def test_capture_real_f4_trace_binds_shas_and_removed_line():
    """Regression: a real T3 trace. The sha is bound by position in git_log (never by message); unrelated steps are pruned."""
    fx = _f4_fixture()
    pb = crystallize.capture(fx["trace"], fx["proposal"], fx["seed"], signature="673bdb8bab80")
    assert pb is not None and pb.t1_blocked is None
    assert [s.tool for s in pb.steps] == ["k8s_describe_pod", "k8s_logs", "git_read", "git_log", "git_diff"]
    (diff_step,) = [s for s in pb.steps if s.tool == "git_diff"]
    assert diff_step.args["sha"].startswith("${sha_")
    by_var = {e.var: e for e in pb.extracts}
    sha_rule = by_var[diff_step.args["sha"][2:-1]].rule
    assert "message" not in sha_rule["pattern"] and "revert" not in sha_rule["pattern"]
    log_text = next(c["result"] for c in crystallize.kept_calls(fx["trace"]) if c["tool"] == "git_log")
    assert crystallize.apply_rule(sha_rule, log_text) == "9a9c251b502211167b9ec6e29ef8ee2fd31a1891"  # first entry, not index 2
    assert next(s for s in pb.steps if s.tool == "git_log").args["path"] == fx["proposal"]["file"]
    db = by_var["db_url"]
    assert db.rule["type"] == "diff_removed_line"
    assert pb.steps[db.from_step].tool == "git_diff"


async def test_t1_replays_real_f4_playbook_on_fresh_incident_with_other_shas():
    fx = _f4_fixture()
    pb = crystallize.capture(fx["trace"], fx["proposal"], fx["seed"], signature="673bdb8bab80")
    kept = {c["tool"]: c["result"] for c in crystallize.kept_calls(fx["trace"])}
    new = ["a1" * 20, "b2" * 20, "c3" * 20, "d4" * 20]
    log_text = "\n".join(json.dumps({"sha": sh, "date": "2027-01-01T00:00:00+00:00", "message": f"unrelated {n}"})
                         for n, sh in enumerate(new))
    old_diff = next(c["result"] for c in crystallize.kept_calls(fx["trace"]) if c["tool"] == "git_diff"
                    and "-  DB_URL" in c["result"])

    class Mcp:
        async def call(self, name, args):
            if name == "git_log":
                return log_text, False
            if name == "git_diff":
                assert args["sha"] == new[0]
                return old_diff.replace("3f385c272d7cbbed59ff3cbf8e33fc8a498ca9ce", new[0]), False
            if name == "git_read" and args["path"] == "deploy/demo/configmaps.yaml":
                return next(c["result"] for c in crystallize.kept_calls(fx["trace"]) if c["tool"] == "git_read"
                            and c["args"]["path"] == "deploy/demo/configmaps.yaml"), False
            return kept.get(name, "x"), False

    seed = {**fx["seed"], "name": "api-5c7d9f8b6-zzzzz"}
    res = await t1.run(pb, seed, Mcp(), Tracer())
    assert res.file == fx["proposal"]["file"] and res.edits == fx["proposal"]["edits"]


async def test_t1_already_fixed_file_raises_already_fixed_not_tier_error():
    from repair_operator.models import AlreadyFixed

    pb = captured()
    mcp = FakeMcp()
    fixed = CM.replace("  LOG_LEVEL: info", EDIT["replace"])

    async def call(name, args):
        return (fixed, False) if name == "git_read" else await FakeMcp.call(mcp, name, args)

    mcp.call = call
    with pytest.raises(AlreadyFixed):
        await t1.run(pb, {**SEED, "name": "api-9z8-qwert"}, mcp, Tracer())
