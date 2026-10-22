import shutil
import subprocess
from pathlib import Path

import pytest

from repair_operator import guardrails as g
from repair_operator.guardrails import GuardrailError, apply_edits, check
from repair_operator.models import Edit, Proposal

FIX = Path(__file__).parent / "fixtures"
API_PATH = "deploy/demo/api.yaml"
API = (FIX / "api.yaml").read_text()
WF_PATH = "workflows/release-pipeline.yaml"
WF = (FIX / "release-pipeline.yaml").read_text()

CMD_LINE = '          command: ["python", "/app/app.py"]'
SPEC_LINE = "    spec:\n      containers:"
NO_CLUSTER = ("server_dry_run",)
POLICY_ONLY = ("server_dry_run", "static")

needs_static = pytest.mark.skipif(
    not (shutil.which("yamllint") and shutil.which("kubeconform")), reason="yamllint/kubeconform missing"
)
needs_conftest = pytest.mark.skipif(not shutil.which("conftest"), reason="conftest missing")


def prop(edits, file=API_PATH):
    return Proposal(
        **{
            "class": "oom",
            "root_cause": "x",
            "confidence": 0.9,
            "fixable_in_git": True,
            "file": file,
            "edits": edits,
            "tier": 1,
            "source": "rules",
        }
    )


def run(edits, text=API, path=API_PATH, file=None, skip=NO_CLUSTER):
    new = text
    try:
        new = apply_edits(text, edits)
    except GuardrailError:
        pass
    return check(prop(edits, file or path), text, new, path, skip=skip)


def mem(value):
    return [Edit(find="              memory: 128Mi", replace=f"              memory: {value}")]


# ---- apply_edits / exact_edit --------------------------------------------------


def test_apply_edits_exact():
    assert "256Mi" in apply_edits(API, mem("256Mi"))


def test_apply_edits_missing_or_ambiguous_raises():
    with pytest.raises(GuardrailError, match="exact_edit"):
        apply_edits(API, [Edit(find="nope-not-there", replace="x")])
    with pytest.raises(GuardrailError, match="exact_edit"):
        apply_edits(API, [Edit(find="app: api", replace="app: web")])  # occurs many times


def test_exact_edit_rejects_mismatched_new_text():
    r = check(prop(mem("256Mi")), API, API + "# extra\n", API_PATH, skip=NO_CLUSTER)
    assert r.rejected_by == "exact_edit"


def test_exact_edit_rejects_ambiguous_find():
    assert run([Edit(find="app: api", replace="app: web")]).rejected_by == "exact_edit"


# ---- path_allowlist ------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["src/evil.yaml", "deploy/demo/api.json", "../deploy/x.yaml", "deploy/../etc/x.yaml", "/deploy/a.yaml"],
)
def test_path_allowlist_rejects_bad_paths(path):
    assert run(mem("256Mi"), path=path).rejected_by == "path_allowlist"


def test_path_must_equal_evidence_file():
    assert run(mem("256Mi"), file="deploy/demo/worker.yaml").rejected_by == "path_allowlist"


# ---- size_cap ------------------------------------------------------------------


def test_size_cap():
    assert g.size_cap(API, apply_edits(API, mem("256Mi")))[0]
    big = [Edit(find=CMD_LINE, replace="\n".join(f"          # c{i}" for i in range(11)))]
    r = run(big)
    assert r.rejected_by == "size_cap"
    assert "changed lines" in r.details


# ---- static --------------------------------------------------------------------


@needs_static
def test_static_ok_and_bad_yaml():
    assert g.static(API, API_PATH)[0]
    ok, msg = g.static(API + "\nkey: [unclosed\n", API_PATH)
    assert not ok and "yamllint" in msg


@needs_static
def test_static_kubeconform_strict_rejects_unknown_field():
    r = run([Edit(find=CMD_LINE, replace=CMD_LINE + "\n          bogusField: 1")])
    assert r.rejected_by == "static"


def test_static_fails_closed_when_binary_missing(monkeypatch):
    monkeypatch.setattr(g.shutil, "which", lambda name: None)
    ok, msg = g.static(API, API_PATH)
    assert not ok and "not found" in msg


@pytest.mark.skipif(not shutil.which("argo"), reason="argo missing")
@needs_static
def test_static_workflow_template_lint_runs():
    _ok, msg = g.static(WF, WF_PATH)
    assert "argo lint" in msg


# ---- server_dry_run ------------------------------------------------------------


def test_server_dry_run_skipped_without_cluster(monkeypatch):
    monkeypatch.setattr(g, "cluster_reachable", lambda ctx=g.KUBE_CONTEXT: False)
    assert g.server_dry_run(API, API_PATH) == (True, "skipped: no cluster")


def test_server_dry_run_failure_reported(monkeypatch):
    monkeypatch.setattr(g, "cluster_reachable", lambda ctx=g.KUBE_CONTEXT: True)
    monkeypatch.setattr(g, "_run", lambda cmd, timeout=60: subprocess.CompletedProcess(cmd, 1, "", "denied"))
    ok, msg = g.server_dry_run(API, API_PATH)
    assert not ok and "denied" in msg


# ---- policy --------------------------------------------------------------------


@needs_conftest
def test_policy_accepts_small_memory_bump():
    r = run(mem("256Mi"), skip=POLICY_ONLY)
    assert r.ok, r.details


@needs_conftest
def test_policy_rejects_limit_over_4x():
    r = run(mem("1Gi"), skip=POLICY_ONLY)
    assert r.rejected_by == "policy" and "4x" in r.details


@needs_conftest
def test_policy_prompt_injection_privileged_rejected():
    inj = [Edit(find=CMD_LINE, replace=CMD_LINE + "\n          securityContext:\n            privileged: true")]
    r = run(inj)
    assert r.rejected_by == "policy" and "privileged" in r.details


@needs_conftest
def test_policy_rejects_registry_change():
    e = [Edit(find="image: python:3.12-alpine", replace="image: evil.io/python:3.12-alpine")]
    r = run(e, skip=POLICY_ONLY)
    assert r.rejected_by == "policy" and "registry" in r.details


@needs_conftest
def test_policy_rejects_removed_probe():
    probe = (
        "          readinessProbe:\n            httpGet:\n              path: /healthz\n"
        "              port: 8000\n            periodSeconds: 5\n"
    )
    r = run([Edit(find=probe, replace="")], skip=POLICY_ONLY)
    assert r.rejected_by == "policy" and "readinessProbe" in r.details


@needs_conftest
def test_policy_rejects_secret_literal():
    find = '              value: "1.0.0"\n          envFrom'
    rep = '              value: "1.0.0"\n            - name: DB_PASSWORD\n              value: hunter2\n          envFrom'
    r = run([Edit(find=find, replace=rep)], skip=POLICY_ONLY)
    assert r.rejected_by == "policy" and "secret" in r.details


@needs_conftest
def test_policy_rejects_hostnetwork_and_service_account():
    e = [Edit(find=SPEC_LINE, replace="    spec:\n      hostNetwork: true\n      containers:")]
    r = run(e, skip=POLICY_ONLY)
    assert r.rejected_by == "policy" and "hostNetwork" in r.details
    e = [Edit(find=SPEC_LINE, replace="    spec:\n      serviceAccountName: admin\n      containers:")]
    r = run(e, skip=POLICY_ONLY)
    assert r.rejected_by == "policy" and "serviceAccountName" in r.details


def test_policy_fails_closed_when_conftest_missing(monkeypatch):
    monkeypatch.setattr(g.shutil, "which", lambda name: None)
    ok, msg = g.policy(API, API, API_PATH)
    assert not ok and "not found" in msg


@needs_conftest
@needs_static
def test_full_pipeline_passes_for_valid_fix():
    r = run(mem("256Mi"))
    assert r.ok and r.rejected_by is None, r.details
