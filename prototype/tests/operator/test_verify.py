from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from repair_operator import incidents, knowledge, retrigger
from repair_operator import verify_handlers as vh

T0 = datetime(2026, 11, 3, 10, 0, 0, tzinfo=UTC)
SHA = "abc123"


def ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeCustom:
    def __init__(self, app=None, workflow=None):
        self.app = app or {}
        self.workflow = workflow or {}
        self.created, self.patched = [], []

    def patch_namespaced_custom_object(self, g, v, ns, plural, name, body):
        self.patched.append((ns, plural, name, body))

    def get_namespaced_custom_object(self, g, v, ns, plural, name):
        return self.app if plural == "applications" else self.workflow

    def create_namespaced_custom_object(self, g, v, ns, plural, body):
        self.created.append(body)
        return {"metadata": {"name": body["metadata"]["generateName"] + "x1"}}


class FakeCore:
    def __init__(self, pods):
        self.pods = pods

    def list_namespaced_pod(self, ns):
        return SimpleNamespace(items=self.pods)


def synced(sha=SHA, health="Healthy"):
    return {"status": {"sync": {"status": "Synced", "revision": sha}, "health": {"status": health}}}


def pod(created, ready=True, restarts=0, waiting=None, name="api-5d9f-abcde", rs="api-5d9f"):
    cs = {"name": "api", "restartCount": restarts, "state": {"waiting": {"reason": waiting}} if waiting else {}}
    return {
        "metadata": {"name": name, "creationTimestamp": ts(created),
                     "ownerReferences": [{"kind": "ReplicaSet", "name": rs}]},
        "status": {"containerStatuses": [cs],
                   "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


def incident(kind="Pod", phase="Merged", **status):
    source = {"kind": "Pod", "name": "api-old", "owner": "Deployment/api"} if kind == "Pod" else {
        "kind": "Workflow", "name": "wf1", "template": "build"}
    return {
        "metadata": {"name": "ri-1", "namespace": "demo"},
        "spec": {"source": source, "fingerprint": "fp1"},
        "status": {"phase": phase, "attempts": 1, "mergeSha": SHA, "timestamps": {"merged": ts(T0)}, **status},
    }


# --- Merged ---


def test_merged_waits_then_enters_verifying():
    c = FakeCustom(app=synced("other"))
    p: dict = {}
    assert vh.merged_step(incident(), p, c, T0 + timedelta(seconds=30)) == vh.SYNC_POLL_SECONDS and p == {}
    assert c.patched[0][3]["metadata"]["annotations"]["argocd.argoproj.io/refresh"] == "normal"
    c.app = synced()
    assert vh.merged_step(incident(), p, c, T0 + timedelta(seconds=40)) == 0
    assert p["status"]["phase"] == "Verifying" and p["status"]["verify"]["healthySince"] is None
    assert c.created == []  # deployment fix needs no re-run


def test_merged_workflow_resubmits_with_label():
    c = FakeCustom(app=synced())
    p: dict = {}
    vh.merged_step(incident("Workflow"), p, c, T0)
    wf = c.created[0]
    assert wf["metadata"]["labels"] == {"repair.example.dev/incident": "ri-1"}
    assert wf["spec"]["workflowTemplateRef"]["name"] == "build"
    assert p["status"]["verify"]["rerun"] == "build-repair-x1"


def test_merged_sync_timeout_and_missing_sha():
    p: dict = {}
    vh.merged_step(incident(), p, FakeCustom(), T0 + timedelta(seconds=601))
    assert p["status"]["phase"] == "Failed" and p["status"]["outcome"]["reason"] == "sync_timeout"
    p = {}
    inc = incident()
    del inc["status"]["mergeSha"]
    vh.merged_step(inc, p, FakeCustom(), T0)
    assert p["status"]["outcome"]["reason"] == "missing_merge_sha"
    inc["status"]["mergedSha"] = SHA  # alias accepted
    assert vh.merge_sha(inc["status"]) == SHA


# --- Verifying ---


def verifying(**verify):
    return incident(verify={"startedAt": ts(T0), **verify})


def test_deployment_window_to_healed():
    core = FakeCore([pod(T0 - timedelta(minutes=5), ready=False, restarts=9, name="api-old-1", rs="api-old"),
                     pod(T0 + timedelta(seconds=10))])
    c = FakeCustom(app=synced())
    p: dict = {}
    t1 = T0 + timedelta(seconds=20)
    assert vh.verify_step(verifying(), p, core, c, window=120, max_attempts=3, now=t1) == "healthy"
    assert p["status"]["verify"]["healthySince"] == ts(t1)
    p = {}
    t2 = t1 + timedelta(seconds=121)
    assert vh.verify_step(verifying(healthySince=ts(t1)), p, core, c, 120, 3, t2) == "healed"
    assert p["status"]["phase"] == "Healed" and p["status"]["outcome"]["result"] == "healed"


def test_deployment_waits_when_not_ready_and_times_out():
    core = FakeCore([pod(T0 + timedelta(seconds=5), ready=False)])
    c = FakeCustom(app=synced("x", health="Progressing"))
    p: dict = {}
    assert vh.verify_step(verifying(), p, core, c, 120, 3, T0 + timedelta(seconds=30)) == "waiting"
    assert p == {}
    assert vh.verify_step(verifying(), p, core, c, 120, 3, T0 + timedelta(seconds=800)) == "failed"


def test_failure_goes_back_to_analyzing_then_escalates():
    core = FakeCore([pod(T0 + timedelta(seconds=5), restarts=2, waiting="CrashLoopBackOff")])
    c = FakeCustom(app=synced())
    inc = verifying()
    inc["status"]["reviewFeedback"] = ["earlier"]
    p: dict = {}
    assert vh.verify_step(inc, p, core, c, 120, 3, T0 + timedelta(seconds=30)) == "failed"
    assert p["status"]["phase"] == "Analyzing"
    assert p["status"]["reviewFeedback"][0] == "earlier"
    assert p["status"]["reviewFeedback"][1].startswith("re-run failed")
    inc["status"]["attempts"] = 3
    p = {}
    vh.verify_step(inc, p, core, c, 120, 3, T0 + timedelta(seconds=30))
    assert p["status"]["phase"] == "Escalated" and p["status"]["outcome"]["result"] == "escalated"


def test_workflow_verdicts():
    inc = verifying(rerun="build-repair-x1")
    inc["spec"]["source"] = {"kind": "Workflow", "name": "wf1", "template": "build"}
    for phase, want in [("Succeeded", "Healed"), ("Failed", "Analyzing"), ("Running", None)]:
        p: dict = {}
        vh.verify_step(inc, p, None, FakeCustom(workflow={"status": {"phase": phase}}), 120, 3, T0)
        assert p.get("status", {}).get("phase") == want


def test_repeated_verify_failures_end_in_escalated():
    inc = verifying(rerun="build-repair-x1")
    inc["spec"]["source"] = {"kind": "Workflow", "name": "wf1", "template": "build"}
    inc["status"]["attempts"] = 1
    c = FakeCustom(workflow={"status": {"phase": "Failed"}})
    phases = []
    for _ in range(5):
        p: dict = {}
        vh.verify_step(inc, p, None, c, 120, 3, T0)
        phases.append(p["status"]["phase"])
        if p["status"]["phase"] == "Escalated":
            break
        inc["status"].update(p["status"])
        inc["status"]["phase"] = "Verifying"  # the next attempt got merged and is verifying again
        inc["status"]["verify"] = {"startedAt": ts(T0), "rerun": "build-repair-x1"}
    assert phases == ["Analyzing", "Analyzing", "Escalated"]
    assert inc["status"]["attempts"] == 3 and p["status"]["attempts"] == 4
    assert p["status"]["outcome"]["result"] == "escalated"


# --- Closing and learner ---


class FakeRepo:
    def __init__(self, text):
        self.text = text

    def get_contents(self, path, ref):
        return SimpleNamespace(decoded_content=self.text.encode())


def healed(template="raise-memory"):
    inc = incident(phase="Healed")
    inc["status"].update(
        signature="sig1", classification={"class": "OOM_KILLED"},
        proposal={"file": "deploy/demo/api.yaml", "template": template, "params": {"memory": "256Mi"},
                  "edits": [{"find": "memory: 32Mi", "replace": "memory: 256Mi"}]},
    )
    return inc


def test_edits_applied():
    e = [{"find": "memory: 32Mi", "replace": "memory: 256Mi"}]
    assert vh.edits_applied("limits:\n  memory: 256Mi\n", e)
    assert not vh.edits_applied("memory: 512Mi", e)
    assert not vh.edits_applied("memory: 256Mi\nmemory: 32Mi", e)


def test_learner_records_healed_and_promotes(tmp_path):
    store = knowledge.RecipeStore(tmp_path / "k.db", promotion_threshold=2)
    for _ in range(2):
        p: dict = {}
        out = vh.learn(healed(), p, repo=FakeRepo("memory: 256Mi"), store=store)
        assert out == "healed" and p["status"]["learning"]["mergedMatchesProposal"] is True
    assert store.lookup("sig1").template == "raise-memory"


def test_learner_reviewer_changed_file_gives_no_credit(tmp_path):
    store = knowledge.RecipeStore(tmp_path / "k.db", promotion_threshold=1)
    p: dict = {}
    assert vh.learn(healed(), p, repo=FakeRepo("memory: 512Mi"), store=store) == "approved"
    assert store.lookup("sig1") is None and store.get("sig1") is None or store.get("sig1").rerun_passed == 0


def test_learner_failure_demotes_and_is_idempotent(tmp_path):
    store = knowledge.RecipeStore(tmp_path / "k.db", promotion_threshold=1)
    vh.learn(healed(), {}, repo=FakeRepo("memory: 256Mi"), store=store)
    assert store.lookup("sig1")
    bad = healed()
    bad["status"]["phase"] = "Escalated"
    assert vh.learn(bad, {}, store=store) == "failed"
    assert store.get("sig1").status == "demoted"
    bad["status"]["learning"] = {"recorded": True}
    assert vh.learn(bad, {}, store=store) is None


def test_learner_skips_without_template_or_signature(tmp_path):
    store = knowledge.RecipeStore(tmp_path / "k.db")
    inc = healed(template=None)
    assert vh.learn(inc, {}, store=store) is None
    inc = healed()
    del inc["status"]["signature"]
    assert vh.learn(inc, {}, store=store) is None


def test_closed_starts_cooldown(monkeypatch):
    incidents.clear_cooldowns()
    vh.closed_step(incident(phase="Dismissed"), {})
    assert incidents.in_cooldown("fp1")
    incidents.clear_cooldowns()


def test_retrigger_stub_compat_and_helpers():
    assert retrigger.retrigger({}) == {}
    assert retrigger.app_name("demo-b") == "demo-app-b"
