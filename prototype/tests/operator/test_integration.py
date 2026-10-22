"""Batch 3 integration: a full happy path with fakes writes one metrics event per phase."""

import ast
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from test_pr_flow import NAME, SKIP, make_env, merge_patch, proposal
from test_verify import SHA, T0, FakeCore, FakeCustom, pod, synced

from repair_operator import main, metrics, repair_flow
from repair_operator import verify_handlers as vh


def test_happy_path_emits_one_event_per_phase(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "get_settings", lambda: SimpleNamespace(state_dir=tmp_path / "state"))
    env, cur = make_env(tmp_path, lambda b, f=None, **k: proposal(find=cur, replace="memory: 1Gi"))
    body = env.body
    body["metadata"]["namespace"] = "demo"
    body["spec"] = {"fingerprint": "fp1", "source": None}
    body["status"] = {"attempts": 0, "evidenceRef": "sha256:x", "signature": "sig",
                      "timestamps": {"detected": "2026-11-03T09:00:00Z"}}

    def step(fn):
        p: dict = {}
        fn(p)
        merge_patch(body["status"], p.get("status", {}))

    step(lambda p: main.incident_created(body, p))
    step(lambda p: main.collect_step(body, p, 0))  # no source: straight to Collected
    body["spec"]["source"] = {"kind": "Pod", "name": "api-old", "owner": "Deployment/api"}
    step(lambda p: main.analyze_hook(body, p))
    step(lambda p: repair_flow.analyze_step(
        body, p, repo=env.repo, analyze_fn=env.analyze_fn, guard_skip=SKIP, settings=env.settings,
        collect_fn=env.collect_fn))
    step(lambda p: repair_flow.propose_step(body, p, repo=env.repo, settings=env.settings))
    env.repo.prs[0].merge()
    step(lambda p: repair_flow.review_step(body, p, repo=env.repo, settings=env.settings))
    assert body["status"]["mergeSha"] == "mergesha1"
    body["status"]["mergeSha"] = SHA  # what Argo CD reports as synced
    custom = FakeCustom(app=synced())
    step(lambda p: vh.merged_step(body, p, custom, T0))
    core = FakeCore([pod(T0 - timedelta(hours=1))])
    step(lambda p: vh.verify_step(body, p, core, custom, 120, 3, T0 + timedelta(seconds=10)))
    step(lambda p: vh.verify_step(body, p, core, custom, 120, 3, T0 + timedelta(seconds=200)))
    assert body["status"]["phase"] == "Healed"

    events = [json.loads(x) for x in (tmp_path / "state" / "events.jsonl").read_text().splitlines()]
    assert [e["phase"] for e in events] == [
        "Detected", "Collected", "Analyzing", "Proposed", "AwaitingReview", "Merged", "Verifying", "Healed",
    ]
    assert {e["incident"] for e in events} == {NAME} and {e["namespace"] for e in events} == {"demo"}
    assert events[3]["class"] == "CRASH_LOOP" and events[3]["tier"] == 3


def test_every_set_phase_call_passes_the_incident():
    src = Path(repair_flow.__file__).parent
    missing = []
    for f in ("detectors.py", "evidence.py", "main.py", "repair_flow.py", "verify_handlers.py"):
        for node in ast.walk(ast.parse((src / f).read_text())):
            is_set_phase = isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "set_phase"
            if is_set_phase and not any(k.arg == "incident" for k in node.keywords):
                missing.append(f"{f}:{node.lineno}")
    assert missing == []
