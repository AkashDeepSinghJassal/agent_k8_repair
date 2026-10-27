import json
from types import SimpleNamespace as NS

import pytest

from repair_mcp import argo_server, git_server, k8s_server, repair_server
from repair_mcp.k8s_server import Apis


class FakeCore:
    def __init__(self):
        self.calls = []

    def list_namespaced_pod(self, ns, **kw):
        return {"items": [{"metadata": {"name": "api-1"}, "status": {"phase": "Running", "containerStatuses": [
            {"name": "a", "ready": False, "restartCount": 3, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}]}

    def read_namespaced_pod(self, name, ns):
        return {"metadata": {"name": name, "ownerReferences": [{"kind": "ReplicaSet", "name": "rs"}]},
                "spec": {"nodeName": "n1", "containers": [{"name": "a", "image": "x:1"}]},
                "status": {"phase": "Running", "containerStatuses": [{"name": "a", "restartCount": 3,
                           "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                           "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]}}

    def read_namespaced_secret(self, name, ns):
        return {"metadata": {"name": name, "namespace": ns, "managedFields": [1]},
                "data": {"password": "c3VwZXJzZWNyZXQ="}, "type": "Opaque"}

    def read_namespaced_pod_log(self, pod, ns, **kw):
        self.calls.append(kw)
        return "start\npassword=hunter2\n" + "\n".join(f"l{i}" for i in range(300))

    def list_namespaced_event(self, ns, **kw):
        return {"items": [{"type": "Warning", "reason": f"R{i}", "message": "m", "lastTimestamp": f"2026-01-01T00:00:{i:02d}Z",
                           "involvedObject": {"kind": "Pod", "name": "p"}} for i in range(60)]}


@pytest.fixture
def apis():
    return Apis(core=FakeCore())


def test_namespace_allowlist(apis):
    assert k8s_server.k8s_list("pods", "kube-system", apis=apis) == {"error": "namespace not allowed"}
    assert k8s_server.k8s_logs("kube-system", "p", apis=apis) == {"error": "namespace not allowed"}
    assert k8s_server.k8s_list("pods", "demo", apis=apis)[0]["name"] == "api-1"


def test_unknown_kind(apis):
    assert "error" in k8s_server.k8s_list("nodes", "demo", apis=apis)


def test_secret_values_never_returned(apis):
    out = k8s_server.k8s_get("secrets", "demo", "s", apis=apis)
    assert out["keys"] == ["password"]
    assert "c3VwZXJzZWNyZXQ=" not in json.dumps(out)


def test_describe_pod(apis):
    out = k8s_server.k8s_describe_pod("demo", "api-1", apis=apis)
    assert out["containers"][0]["lastState"]["reason"] == "OOMKilled"
    assert out["owner"] == [{"kind": "ReplicaSet", "name": "rs"}]


def test_logs_redacted_and_truncated(apis):
    out = k8s_server.k8s_logs("demo", "p", apis=apis)
    assert "hunter2" not in out and "truncated" in out and len(out.splitlines()) <= 201


def test_events_sorted_capped(apis):
    ev = k8s_server.k8s_events("demo", apis=apis)
    assert len(ev) == 50 and ev[0]["reason"] == "R59"


class FakeRepo:
    def __init__(self):
        self.refs = []

    def get_contents(self, path, ref):
        self.refs.append(ref)
        return NS(decoded_content=b"a: 1\ntoken: abc123\n")

    def get_git_tree(self, branch, recursive):
        return NS(tree=[NS(path=p, type="blob") for p in ["deploy/demo/api.yaml", "deploy/x.txt", "README.md"]])

    def get_commits(self, path, sha):
        f = NS(filename=path, patch="@@ -1 +1 @@\n-a\n+b")
        c = NS(sha="a" * 40, files=[f], commit=NS(message="fix it\nbody", author=NS(date=NS(isoformat=lambda: "2026-01-01"))))
        return [c]

    def get_commit(self, sha):
        return self.get_commits("deploy/a.yaml", "main")[0]


def test_git_path_allowlist():
    r = FakeRepo()
    for bad in ["../secrets.yaml", "deploy/../x.yaml", "README.md", "src/a.yaml", "deploy/a.json"]:
        assert "error" in git_server.git_read(bad, client=r)
    assert "error" in git_server.git_log("README.md", client=r)
    assert "error" in git_server.git_diff("src/a.yaml", client=r)


def test_git_read_redacts_and_repo_allowlist():
    r = FakeRepo()
    assert "abc123" not in git_server.git_read("deploy/demo/api.yaml", client=r)
    assert git_server.git_read("deploy/demo/api.yaml", repo="evil/repo", client=r) == {"error": "repo not allowed"}


def test_git_list_log_diff():
    r = FakeRepo()
    assert git_server.git_list(client=r) == ["deploy/demo/api.yaml"]
    assert git_server.git_log("deploy/a.yaml", client=r)[0]["message"] == "fix it"
    assert "+b" in git_server.git_diff("deploy/a.yaml", client=r)


class FakeCustom:
    def list_namespaced_custom_object(self, g, v, ns, plural):
        return {"items": [{"metadata": {"name": "demo-app"}, "spec": {"destination": {"namespace": "demo"},
                "source": {"repoURL": "r", "path": "deploy/demo", "targetRevision": "main"}}}]}

    def get_namespaced_custom_object(self, g, v, ns, plural, name):
        if plural == "workflows":
            return {"status": {"phase": "Failed", "nodes": {"n": {"name": "w", "displayName": "d", "type": "Pod", "id": "w-1", "phase": "Failed"}}}}
        return {"spec": {"destination": {"namespace": "kube-system" if name == "bad" else "demo"}},
                "status": {"sync": {"status": "Synced", "revision": "abc"}, "health": {"status": "Degraded"},
                           "resources": [{"kind": "Deployment", "name": "api", "status": "Synced", "health": {"status": "Degraded", "message": "x"}}]}}


def test_argo():
    api = FakeCustom()
    assert argo_server.argo_app_for_namespace("demo", api=api)["app"] == "demo-app"
    assert "error" in argo_server.argo_app_for_namespace("other", api=api)
    assert argo_server.argo_app_status("demo-app", api=api)["resources"][0]["health"] == "Degraded"
    assert "error" in argo_server.argo_app_status("bad", api=api)
    assert argo_server.workflow_status("demo", "wf", api=api)["nodes"][0]["podName"] == "w-1"
    assert "error" in argo_server.workflow_status("kube-system", "wf", api=api)


GOOD = {"file": "deploy/demo/api.yaml", "edits": [{"find": "a", "replace": "b"}], "root_cause": "rc",
            "failure_class": "OOM_KILLED", "confidence": 0.9, "rationale": "r"}


def test_propose_patch_writes(tmp_path):
    out = tmp_path / "o" / "out.json"
    assert "stop now" in repair_server.propose_patch(**GOOD, out=str(out))
    data = json.loads(out.read_text())
    assert data["status"] == "proposed" and data["edits"] == GOOD["edits"] and data["file"] == GOOD["file"]


@pytest.mark.parametrize("change", [
    {"edits": []}, {"edits": [{"find": "a", "replace": "a"}]}, {"edits": [{"find": "", "replace": "a"}]},
    {"file": "src/x.yaml"}, {"failure_class": "NOPE"}, {"confidence": 1.5},
])
def test_propose_patch_invalid(tmp_path, change):
    out = tmp_path / "out.json"
    res = repair_server.propose_patch(**{**GOOD, **change}, out=str(out))
    assert "error" in res and not out.exists()


def test_escalate(tmp_path):
    out = tmp_path / "out.json"
    assert "stop now" in repair_server.escalate("needs human", failure_class="OTHER", out=str(out))
    assert json.loads(out.read_text())["status"] == "escalated"


def test_no_out_env(monkeypatch):
    monkeypatch.delenv("REPAIR_AGENT_OUT", raising=False)
    assert "error" in repair_server.escalate("x")
