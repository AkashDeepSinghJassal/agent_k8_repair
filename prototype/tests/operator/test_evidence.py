import json
from datetime import UTC, datetime
from types import SimpleNamespace

import jsonschema
import pytest

from repair_operator import evidence, main

NOW = datetime(2026, 11, 3, 10, 1, 5, tzinfo=UTC)


class ApiErr(Exception):
    def __init__(self, status):
        self.status = status


def pod(ready=False, waiting="CrashLoopBackOff", restarts=6):
    state = {} if ready else {"waiting": {"reason": waiting}}
    return {
        "metadata": {"name": "api-1", "annotations": {"repair.example.dev/source-path": "deploy/demo/api.yaml"}},
        "spec": {
            "containers": [
                {
                    "name": "api",
                    "image": "ghcr.io/x/api:1",
                    "resources": {"limits": {"memory": "128Mi"}},
                    "env": [{"name": "DB_URL", "value": "postgres://u:hunter2@db"}, {"name": "LOG"}],
                    "envFrom": [{"configMapRef": {"name": "api-config"}}, {"secretRef": {"name": "s"}}],
                }
            ]
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "containerStatuses": [
                {
                    "name": "api",
                    "ready": ready,
                    "restartCount": restarts,
                    "state": state,
                    "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
                }
            ],
        },
    }


class FakeCore:
    def __init__(self, pod_obj=None, events=None, current="", previous=None):
        self.pod, self.events, self.current, self.previous = pod_obj, events or [], current, previous
        self.log_calls = []

    def read_namespaced_pod(self, name, ns):
        if self.pod is None:
            raise ApiErr(404)
        return self.pod

    def list_namespaced_event(self, ns, field_selector=None):
        return {"items": self.events}

    def read_namespaced_pod_log(self, name, ns, **kw):
        self.log_calls.append(kw)
        if kw.get("previous"):
            if self.previous is None:
                raise ApiErr(400)
            return self.previous
        return self.current


class FakeCustom:
    def __init__(self, wf):
        self.wf = wf

    def get_namespaced_custom_object(self, group, version, ns, plural, name):
        assert plural == "workflows"
        return self.wf


class FakeRepo:
    full_name = "o/repo"

    def __init__(self, file_text="a: 1\n", patch="@@ -1 +1 @@\n-a: 0\n+a: 1"):
        self.file_text, self.patch = file_text, patch

    def get_contents(self, path, ref=None):
        return SimpleNamespace(decoded_content=self.file_text.encode(), sha="filesha")

    def get_commits(self, path=None, sha=None):
        f = SimpleNamespace(filename=path, patch=self.patch)
        return [SimpleNamespace(sha="9c1e2ab", files=[f, SimpleNamespace(filename="other", patch="x")])]


def pod_incident(attempts=0):
    return {
        "metadata": {"name": "ri-abc", "namespace": "demo"},
        "spec": {
            "source": {
                "kind": "Pod", "name": "api-1", "owner": "Deployment/api", "container": "api",
                "reason": "OOMKilled",
            }
        },
        "status": {"attempts": attempts},
    }


def wf_incident():
    return {
        "metadata": {"name": "ri-wf", "namespace": "demo"},
        "spec": {"source": {"kind": "Workflow", "name": "wf-1", "template": "build", "node": "test"}},
    }


# --- redaction ---


@pytest.mark.parametrize(
    "text,gone",
    [
        ("password=hunter2 ok", "hunter2"),
        ("DB_PASSWORD: s3cr3t", "s3cr3t"),
        ('{"api_key": "abc def"}', "abc def"),
        ("API-KEY = zzz", "zzz"),
        ("token:abc123", "abc123"),
        ("key AKIAABCDEFGHIJKLMNOP end", "AKIAABCDEFGHIJKLMNOP"),
        ("jwt eyJhbGciOi.eyJzdWIiOiIx.SflKxwRJSMeKKF2QT4", "eyJhbGciOi"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIBOg\nxyz\n-----END RSA PRIVATE KEY-----", "MIIBOg"),
        ("-----BEGIN PRIVATE KEY-----\ntruncated-body", "truncated-body"),
        ("Authorization: Bearer abcdefghijk123", "abcdefghijk123"),
    ],
)
def test_redact(text, gone):
    out = evidence.redact(text)
    assert gone not in out and "[REDACTED]" in out


def test_redact_keeps_normal_text():
    assert evidence.redact("starting server on port 8080") == "starting server on port 8080"


# --- bundle ---


def test_pod_bundle_valid_and_redacted(tmp_path):
    current = "starting\npassword=hunter2\nFATAL: out of memory\n"
    core = FakeCore(
        pod(),
        events=[
            {"type": "Normal", "reason": "Pulled", "message": "ok"},
            {"type": "Warning", "reason": "BackOff", "message": "Back-off token=abc", "count": 14,
             "lastTimestamp": "2026-11-03T10:00:00Z"},
        ],
        current=current,
        previous="prev error line",
    )
    b = evidence.collect(core, None, FakeRepo("secret: leak\nx: 1\n"), pod_incident(), now=NOW)
    evidence.validate(b)
    assert b["spec"]["env"] == ["DB_URL", "LOG"] and "hunter2" not in json.dumps(b)
    assert b["spec"]["envFrom"] == ["configmap/api-config", "secret/s"]
    assert b["failure"] == {"reason": "OOMKilled", "exitCode": 137, "restartCount": 6, "message": None}
    assert [e["reason"] for e in b["events"]] == ["BackOff"] and "abc" not in b["events"][0]["message"]
    assert b["logs"]["previous"] == ["prev error line"] and "leak" not in b["git"]["file"]
    assert b["git"]["sha"] == "9c1e2ab" and b["git"]["path"] == "deploy/demo/api.yaml"
    assert b["git"]["lastCommitDiff"].startswith("--- a/deploy/demo/api.yaml")
    assert "other" not in b["git"]["lastCommitDiff"]
    assert len(b["signature"]) == 12 and b["workflow"] is None and b["collectedAt"] == "2026-11-03T10:01:05Z"

    ref = evidence.collect_and_store(core, None, None, pod_incident(attempts=1), now=NOW, state_dir=tmp_path)
    assert ref["signature"] == b["signature"] and ref["evidenceRef"].startswith("sha256:")
    assert (tmp_path / "evidence" / "ri-abc-2.json").exists()


def test_signature_ignores_volatile_tokens():
    def sig(line):
        core = FakeCore(pod(), current=line)
        return evidence.collect(core, None, None, pod_incident(), now=NOW)["signature"]

    assert sig("error at 2026-01-01T00:00:00Z pod api-7d9f8c6b5d-x2k4q") == sig(
        "error at 2026-02-02T01:01:01Z pod api-5c8d7b4a3f-abcde"
    )
    assert sig("fatal: out of memory") != sig("fatal: connection refused")


def test_caps_enforced():
    events = [
        {"type": "Warning", "reason": f"R{i}", "message": "m", "lastTimestamp": f"2026-11-03T10:00:{i:02d}Z"}
        for i in range(30)
    ]
    big = "\n".join(f"line {i}" for i in range(500))
    core = FakeCore(pod(), events=events, current=big, previous=big)
    b = evidence.collect(core, None, FakeRepo("\n".join(f"l{i}" for i in range(900)), "\n".join("p" * 3 for _ in range(900))), pod_incident(), now=NOW)
    evidence.validate(b)
    assert len(b["events"]) == 20 and b["events"][0]["reason"] == "R29"
    assert len(b["logs"]["current"]) == 200 and b["logs"]["current"][-1] == "line 499"
    assert len(b["logs"]["previous"]) == 200
    assert len(b["git"]["file"].splitlines()) == 400 and len(b["git"]["lastCommitDiff"].splitlines()) == 400
    assert all(c["tail_lines"] == 200 for c in core.log_calls)


def test_error_lines_cap():
    lines = [f"error {i}" for i in range(50)]
    assert evidence.error_lines(lines) == lines[-20:]


def test_git_none():
    b = evidence.collect(FakeCore(pod()), None, None, pod_incident(), now=NOW)
    assert b["git"] is None
    evidence.validate(b)


def test_workflow_bundle():
    wf = {
        "metadata": {"name": "wf-1", "annotations": {"repair.example.dev/source-path": "workflows/build.yaml"}},
        "status": {
            "nodes": {
                "wf-1-123": {"id": "wf-1-123", "type": "Pod", "displayName": "test", "phase": "Failed",
                             "templateName": "run-tests", "message": "Error (exit code 2)",
                             "outputs": {"exitCode": "2"}},
                "wf-1": {"id": "wf-1", "type": "DAG", "displayName": "wf-1", "phase": "Failed"},
            }
        },
    }
    core = FakeCore(current="step failed\ntoken=zzz")
    b = evidence.collect(core, FakeCustom(wf), FakeRepo(), wf_incident(), now=NOW)
    evidence.validate(b)
    assert b["workflow"]["template"] == "run-tests" and b["workflow"]["logs"] == ["step failed", "[REDACTED]"]
    assert b["failure"]["exitCode"] == 2 and b["git"]["path"] == "workflows/build.yaml"
    assert core.log_calls[0]["container"] == "main"


def test_schema_rejects_bad_bundle():
    b = evidence.collect(FakeCore(pod()), None, None, pod_incident(), now=NOW)
    b["events"] = [{"reason": "x", "message": "m", "count": 1}] * 21
    with pytest.raises(jsonschema.ValidationError):
        evidence.validate(b)


# --- handler / dismissed ---


def test_dismissed_when_pod_recovered(tmp_path):
    core = FakeCore(pod(ready=True, restarts=6))
    patch: dict = {}
    evidence.handle_collect(pod_incident(), patch, (core, None, None), NOW)
    assert patch["status"]["phase"] == "Dismissed"
    assert patch["status"]["outcome"]["result"] == "dismissed"
    assert "closed" in patch["status"]["timestamps"]


def test_dismissed_when_pod_gone():
    patch: dict = {}
    evidence.handle_collect(pod_incident(), patch, (FakeCore(None), None, None), NOW)
    assert patch["status"]["phase"] == "Dismissed"


def test_collect_step_collects(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "get_settings", lambda: SimpleNamespace(state_dir=tmp_path, demo_branch="main"))
    body = pod_incident()
    body["status"]["timestamps"] = {"detected": "2026-11-03T10:00:00Z"}
    core = FakeCore(pod(), current="error: boom")
    patch: dict = {}
    assert main.collect_step(body, patch, 60, NOW, clients=(core, None, None)) == 0
    st = patch["status"]
    assert st["phase"] == "Collected" and st["evidenceRef"].startswith("sha256:") and len(st["signature"]) == 12
    assert (tmp_path / "evidence" / "ri-abc-1.json").exists()


def test_collect_step_still_debouncing():
    body = {"status": {"timestamps": {"detected": "2026-11-03T10:01:00Z"}}}
    patch: dict = {}
    assert main.collect_step(body, patch, 60, NOW, clients=(None, None, None)) > 0 and patch == {}
