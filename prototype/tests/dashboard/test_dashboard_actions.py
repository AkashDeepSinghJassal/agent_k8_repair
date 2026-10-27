import pytest
from fastapi.testclient import TestClient

from repair_dashboard import actions
from repair_dashboard.actions import FaultBackend, GitHubPRs, GitHubUnavailable
from repair_dashboard.app import app, get_backend, get_prs, get_reader
from repair_dashboard.kube import FakeKubeReader
from repair_faults import injector

PR_URL = "https://github.com/AkashDeepSinghJassal/repair-demo-app/pull/7"


class FakeGit:
    def __init__(self, files):
        self.files, self.commits = dict(files), []

    def get_file(self, path):
        return self.files[path]

    def commit_file(self, path, text, message):
        self.files[path] = text
        self.commits.append(message)
        return f"sha{len(self.commits)}"


class FakePRs:
    def __init__(self, error=None):
        self.merged, self.comments, self.error = [], [], error

    def _chk(self):
        if self.error:
            raise GitHubUnavailable(self.error)

    def diff(self, number):
        self._chk()
        return [{"filename": "deploy/demo/api.yaml", "status": "modified", "additions": 1, "deletions": 1, "patch": "-a\n+b"}]

    def merge_squash(self, number):
        self._chk()
        self.merged.append(number)
        return "mergesha"

    def comment(self, number, text):
        self._chk()
        self.comments.append((number, text))


class FakeKubectl:
    def __init__(self):
        self.calls = []

    def __call__(self, args, stdin=None):
        self.calls.append(args)
        return ""


def incident(phase="AwaitingReview", pr=PR_URL, **kw):
    return {"name": "ri-1", "namespace": "demo", "phase": phase, "pullRequest": pr, **kw}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("REPAIR_APPROVER", "alice")
    monkeypatch.setenv("REPAIR_DEMO_REPO", "AkashDeepSinghJassal/repair-demo-app")
    monkeypatch.delenv("REPAIR_GITHUB_TOKEN", raising=False)
    fault = injector.get_fault("F1")
    git = FakeGit({fault["target_file"]: _healthy_text(fault)})
    ctx = {"git": git, "kubectl": FakeKubectl(), "prs": FakePRs(), "reader": FakeKubeReader(incidents=[incident()])}
    ctx["backend"] = FaultBackend(git=git, kubectl=ctx["kubectl"])
    app.dependency_overrides[get_backend] = lambda: ctx["backend"]
    app.dependency_overrides[get_prs] = lambda: ctx["prs"]
    app.dependency_overrides[get_reader] = lambda: ctx["reader"]
    ctx["client"] = TestClient(app)
    yield ctx
    app.dependency_overrides.clear()


def _healthy_text(fault):
    return "\n".join(e["find"] for e in fault["breaking_edits"]) + "\n"


def test_faults_list_with_state(env):
    c = env["client"]
    data = c.get("/api/faults").json()
    assert [f["id"] for f in data][:9] == [f"F{i}" for i in range(1, 10)]
    f1 = data[0]
    assert f1["injected"] is False and f1["incident"] is None and f1["expected_class"]
    assert "breaking_edits" not in f1


def test_inject_is_idempotent_and_links_incident(env):
    c = env["client"]
    first = c.post("/api/faults/F1/inject")
    assert first.status_code == 200 and first.json()["status"] == "injected"
    second = c.post("/api/faults/F1/inject")
    assert second.json()["status"] == "already-injected"
    assert len(env["git"].commits) == 1
    env["reader"]._incidents = [incident(createdAt="2999-01-01T00:00:00Z")]
    f1 = c.get("/api/faults").json()[0]
    assert f1["injected"] is True and f1["incident"] == "ri-1"


def test_inject_detects_existing_injection_after_restart(env):
    c = env["client"]
    c.post("/api/faults/F1/inject")
    env["backend"].state.injected.clear()  # simulates a dashboard restart
    assert c.post("/api/faults/F1/inject").json()["status"] == "already-injected"
    assert len(env["git"].commits) == 1


def test_revert(env):
    c = env["client"]
    assert c.post("/api/faults/F1/revert").json()["status"] == "not-injected"
    c.post("/api/faults/F1/inject")
    r = c.post("/api/faults/F1/revert")
    assert r.json()["status"] == "reverted" and len(env["git"].commits) == 2
    assert c.get("/api/faults").json()[0]["injected"] is False


def test_unknown_fault_404(env):
    assert env["client"].post("/api/faults/F99/inject").status_code == 404


def test_faults_listing_works_without_token_or_cluster(env):
    env["reader"].error = "no cluster"
    r = env["client"].get("/api/faults")
    assert r.status_code == 200 and len(r.json()) >= 9


def test_inject_without_token_is_503(env):
    env["backend"].git = None  # injector falls back to env token, which is unset
    r = env["client"].post("/api/faults/F1/inject")
    assert r.status_code == 503 and "REPAIR_GITHUB_TOKEN" in r.json()["detail"]


def test_actions_status_hides_token(env, monkeypatch):
    monkeypatch.setenv("REPAIR_GITHUB_TOKEN", "ghp_secret")
    body = env["client"].get("/api/actions/status").text
    assert "ghp_secret" not in body and '"github":true' in body.replace(" ", "")


def test_diff(env):
    r = env["client"].get("/api/incidents/demo/ri-1/diff")
    assert r.status_code == 200 and r.json()["number"] == 7 and r.json()["files"][0]["patch"] == "-a\n+b"


def test_approve_requires_awaiting_review(env):
    env["reader"]._incidents = [incident(phase="Proposed")]
    r = env["client"].post("/api/incidents/demo/ri-1/approve", json={"confirm": True})
    assert r.status_code == 409 and env["prs"].merged == []


def test_approve_requires_confirm(env):
    r = env["client"].post("/api/incidents/demo/ri-1/approve", json={})
    assert r.status_code == 400 and env["prs"].merged == []


def test_approve_merges_and_records(env):
    r = env["client"].post("/api/incidents/demo/ri-1/approve", json={"confirm": True})
    assert r.status_code == 200 and r.json()["approvedBy"] == "alice" and r.json()["recorded"] is True
    assert env["prs"].merged == [7]
    assert env["reader"].approved == [("demo", "ri-1", "alice")]


def test_approve_unknown_incident_and_foreign_pr(env):
    c = env["client"]
    assert c.post("/api/incidents/demo/nope/approve", json={"confirm": True}).status_code == 404
    env["reader"]._incidents = [incident(pr="https://github.com/evil/other/pull/1")]
    assert c.post("/api/incidents/demo/ri-1/approve", json={"confirm": True}).status_code == 409
    env["reader"]._incidents = [incident(pr=None)]
    assert c.get("/api/incidents/demo/ri-1/diff").status_code == 409


def test_approve_without_token_is_503(env):
    env["prs"].error = actions.TOKEN_MESSAGE
    r = env["client"].post("/api/incidents/demo/ri-1/approve", json={"confirm": True})
    assert r.status_code == 503 and "REPAIR_GITHUB_TOKEN" in r.json()["detail"]
    assert env["reader"].approved == []


def test_real_pr_client_without_token_is_unavailable(monkeypatch):
    monkeypatch.delenv("REPAIR_GITHUB_TOKEN", raising=False)
    with pytest.raises(GitHubUnavailable):
        GitHubPRs().diff(1)


def test_request_changes_posts_retry_comment(env):
    c = env["client"]
    r = c.post("/api/incidents/demo/ri-1/request-changes", json={"text": "use  256Mi\nnot 512Mi"})
    assert r.status_code == 200
    assert env["prs"].comments == [(7, "/repair retry use 256Mi not 512Mi")]
    assert c.post("/api/incidents/demo/ri-1/request-changes", json={"text": "  "}).status_code == 400


def test_rerun_pipeline(env):
    r = env["client"].post("/api/pipelines/rerun")
    assert r.status_code == 200 and r.json()["template"] == "release-pipeline"
    assert env["reader"].submitted == [("demo", "release-pipeline")]


def test_inject_and_revert_refresh_argocd(env):
    c = env["client"]
    assert c.post("/api/faults/F1/inject").json()["argocdRefreshed"] is True
    assert env["reader"].refreshed == ["demo-app"]
    assert c.post("/api/faults/F1/revert").json()["argocdRefreshed"] is True
    assert env["reader"].refreshed == ["demo-app", "demo-app"]


def test_inject_still_succeeds_when_argocd_refresh_fails(env):
    env["reader"].error = "down"  # refresh raises KubeUnavailable; the commit must not be affected
    r = env["client"].post("/api/faults/F1/inject")
    assert r.status_code == 200 and r.json()["status"] == "injected" and r.json()["argocdRefreshed"] is False


def test_restart_service_pods(env):
    env["reader"]._services = [
        {"namespace": "demo", "name": "api", "pods": [{"name": "api-abc"}, {"name": "api-def"}]},
    ]
    c = env["client"]
    r = c.post("/api/services/demo/api/restart")
    assert r.status_code == 200 and r.json()["restarted"] == ["api-abc", "api-def"]
    assert env["reader"].restarted == [("demo", "api")]
    assert c.post("/api/services/demo/nope/restart").status_code == 404
    assert c.post("/api/services/kube-system/api/restart").status_code == 404
    env["reader"].error = "down"
    assert c.post("/api/services/demo/api/restart").status_code == 503


def test_rerun_without_cluster_is_503(env):
    env["reader"].error = "down"
    assert env["client"].post("/api/pipelines/rerun").status_code == 503


def test_pr_number_parsing():
    assert actions.pr_number(PR_URL + "/", "akashdeepsinghjassal/repair-demo-app") == 7
    with pytest.raises(ValueError):
        actions.pr_number("not a url")


def test_approver_default(monkeypatch):
    monkeypatch.delenv("REPAIR_APPROVER", raising=False)
    assert actions.approver()


def test_fault_state_persists_across_restarts(tmp_path):
    from repair_dashboard.actions import FaultState

    path = tmp_path / "sub" / "dashboard-faults.json"
    st = FaultState(path)
    ts = st.mark("f1")
    assert FaultState(path).injected == {"f1": ts}
    st.clear("f1")
    assert FaultState(path).injected == {}
