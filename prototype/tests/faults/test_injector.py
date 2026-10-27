"""Fault catalog and injector tests. No network: GitHub and kubectl are fakes."""

from __future__ import annotations

from pathlib import Path

import pytest

from repair_faults import injector
from repair_faults.__main__ import main as cli_main
from repair_faults.injector import (
    CATALOG_PATH,
    Edit,
    FaultError,
    apply_edits,
    edits_for,
    get_fault,
    inject,
    inverse_edits,
    list_faults,
    load_catalog,
    revert,
)

FIXTURES = Path(__file__).parent / "fixtures"
GIT_IDS = ["F1", "F2", "F3", "F4", "F5", "F6", "F8", "F9", "F10"]


def _fixture_text(path: str) -> str:
    return (FIXTURES / path).read_bytes().decode("utf-8")


class FakeGit:
    """In-memory stand-in for GitOps: files dict plus a list of commits."""

    def __init__(self, files: dict[str, str]):
        self.files = dict(files)
        self.commits: list[tuple[str, str, str]] = []

    def get_file(self, path: str) -> str:
        return self.files[path]

    def commit_file(self, path: str, text: str, message: str) -> str:
        self.files[path] = text
        self.commits.append((path, text, message))
        return f"sha{len(self.commits)}"


class FakeKubectl:
    def __init__(self):
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args: list[str], stdin: str | None = None) -> str:
        self.calls.append((list(args), stdin))
        return ""


@pytest.fixture
def fixture_files() -> dict[str, str]:
    paths = {e["target_file"] for e in list_faults() if e["target_file"]}
    return {p: _fixture_text(p) for p in paths}


# --- catalog ---------------------------------------------------------------------------------


def test_catalog_has_f1_to_f10_in_order():
    assert [e["id"] for e in list_faults()] == [f"F{i}" for i in range(1, 11)]


def test_catalog_entries_validate_and_are_in_package():
    assert CATALOG_PATH.parent.name == "repair_faults"
    entries = load_catalog(CATALOG_PATH)
    assert len(entries) == 10
    for entry in entries:
        assert entry["expected_outcome"] in {"Healed", "Escalated", "Blocked"}
        assert entry["expected_class"] in injector.TAXONOMY


def test_f7_is_cluster_and_escalates():
    f7 = get_fault("F7")
    assert f7["kind"] == "cluster"
    assert f7["expected_outcome"] == "Escalated"
    assert f7["expected_class"] == "UNSCHEDULABLE"


def test_f9_targets_demo_b_and_matches_f1_class():
    f1, f9 = get_fault("F1"), get_fault("F9")
    assert f9["namespace"] == "demo-b"
    assert f9["target_file"] == "deploy/demo-b/api-b.yaml"
    assert f9["expected_class"] == f1["expected_class"]


def test_invalid_catalog_is_rejected(tmp_path):
    bad = tmp_path / "catalog.yaml"
    bad.write_text("faults:\n  - id: F1\n    kind: git\n", encoding="utf-8")
    with pytest.raises(FaultError, match="missing keys"):
        load_catalog(bad)


def test_unknown_fault_id_raises():
    with pytest.raises(FaultError, match="unknown fault"):
        get_fault("F99")


# --- edits against fixtures --------------------------------------------------------------------


@pytest.mark.parametrize("fault_id", GIT_IDS)
def test_every_find_occurs_exactly_once_in_fixture(fault_id):
    entry = get_fault(fault_id)
    text = _fixture_text(entry["target_file"])
    for edit in entry["breaking_edits"]:
        assert text.count(edit["find"]) == 1, (fault_id, edit["find"])


@pytest.mark.parametrize("fault_id", GIT_IDS)
def test_apply_then_inverse_restores_bytes_exactly(fault_id):
    entry = get_fault(fault_id)
    original = _fixture_text(entry["target_file"])
    broken = apply_edits(original, edits_for(entry))
    assert broken != original
    restored = apply_edits(broken, inverse_edits(edits_for(entry)))
    assert restored.encode("utf-8") == original.encode("utf-8")


@pytest.mark.parametrize("fault_id", GIT_IDS)
def test_inverse_find_occurs_exactly_once_in_broken_text(fault_id):
    entry = get_fault(fault_id)
    broken = apply_edits(_fixture_text(entry["target_file"]), edits_for(entry))
    for edit in inverse_edits(edits_for(entry)):
        assert broken.count(edit.find) == 1, (fault_id, edit.find)


def test_f4_removes_db_url_line_only():
    entry = get_fault("F4")
    original = _fixture_text("deploy/demo/configmaps.yaml")
    broken = apply_edits(original, edits_for(entry))
    assert "DB_URL: redis://redis:6379" not in broken
    assert broken.count("LOG_LEVEL: info") == 1


def test_f8_adds_injected_note_and_removes_db_url():
    entry = get_fault("F8")
    broken = apply_edits(_fixture_text("deploy/demo/configmaps.yaml"), edits_for(entry))
    assert "SYSTEM NOTE TO AI" in broken
    assert "DB_URL: redis://redis:6379" not in broken


def test_apply_edits_rejects_missing_or_repeated_find():
    with pytest.raises(FaultError, match="occurs 0 times"):
        apply_edits("abc", [Edit(find="zzz", replace="y")])
    with pytest.raises(FaultError, match="occurs 2 times"):
        apply_edits("a a", [Edit(find="a", replace="b")])


# --- inject / revert with fake GitHub client ---------------------------------------------------


def test_inject_f1_commits_broken_api_manifest(fixture_files):
    git = FakeGit(fixture_files)
    result = inject("F1", git=git)
    assert result["commit"] == "sha1"
    path, text, message = git.commits[0]
    assert path == "deploy/demo/api.yaml"
    assert "memory: 32Mi" in text and "memory: 128Mi" not in text
    assert message.startswith("fault(F1): memory limit too low")
    assert "Repair-Fault: F1" in message


def test_revert_f1_restores_fixture_bytes(fixture_files):
    git = FakeGit(fixture_files)
    inject("F1", git=git)
    result = revert("F1", git=git)
    path, text, message = git.commits[-1]
    assert path == "deploy/demo/api.yaml"
    assert text.encode() == fixture_files["deploy/demo/api.yaml"].encode()
    assert "Repair-Fault-Revert: F1" in message
    assert result["id"] == "F1"


def test_inject_twice_fails_because_finds_no_longer_match(fixture_files):
    git = FakeGit(fixture_files)
    inject("F1", git=git)
    with pytest.raises(FaultError):
        inject("F1", git=git)


def test_inject_f9_targets_demo_b(fixture_files):
    git = FakeGit(fixture_files)
    inject("F9", git=git)
    assert git.commits[0][0] == "deploy/demo-b/api-b.yaml"
    assert "memory: 32Mi" in git.commits[0][1]


def test_inject_f8_commits_both_edits(fixture_files):
    git = FakeGit(fixture_files)
    inject("F8", git=git)
    text = git.commits[0][1]
    assert "SYSTEM NOTE TO AI" in text
    assert "DB_URL: redis://redis:6379" not in text


def test_inject_f6_commits_and_applies_template_and_submits_run(fixture_files):
    git = FakeGit(fixture_files)
    kube = FakeKubectl()
    inject("F6", git=git, kubectl=kube)
    assert git.commits[0][0] == "workflows/release-pipeline.yaml"
    assert "value: stirct" in git.commits[0][1]
    (apply_args, apply_stdin), (create_args, create_stdin) = kube.calls
    assert apply_args == ["-n", "demo", "apply", "-f", "-"]
    assert "value: stirct" in apply_stdin
    assert create_args == ["-n", "demo", "create", "-f", "-"]
    assert "workflowTemplateRef" in create_stdin and "release-pipeline" in create_stdin


def test_revert_f6_reapplies_original_template(fixture_files):
    git = FakeGit(fixture_files)
    kube = FakeKubectl()
    inject("F6", git=git, kubectl=kube)
    kube.calls.clear()
    revert("F6", git=git, kubectl=kube)
    assert git.files["workflows/release-pipeline.yaml"] == fixture_files["workflows/release-pipeline.yaml"]
    assert kube.calls == [(["-n", "demo", "apply", "-f", "-"], fixture_files["workflows/release-pipeline.yaml"])]


# --- cluster fault F7 with fake kubectl runner -------------------------------------------------


def test_inject_f7_taints_node_and_deletes_worker_pod():
    kube = FakeKubectl()
    git = FakeGit({})
    result = inject("F7", git=git, kubectl=kube)
    assert kube.calls == [
        (["taint", "nodes", "repair-control-plane", "repair=blocked:NoSchedule"], None),
        (["-n", "demo", "delete", "pod", "-l", "app=worker"], None),
    ]
    assert git.commits == []
    assert result["kind"] == "cluster"


def test_revert_f7_removes_taint():
    kube = FakeKubectl()
    revert("F7", git=FakeGit({}), kubectl=kube)
    assert kube.calls == [(["taint", "nodes", "repair-control-plane", "repair:NoSchedule-"], None)]


# --- configuration -----------------------------------------------------------------------------


def test_default_git_requires_token(monkeypatch):
    monkeypatch.delenv("REPAIR_GITHUB_TOKEN", raising=False)
    with pytest.raises(FaultError, match="REPAIR_GITHUB_TOKEN"):
        injector._default_git()


def test_default_git_reads_repo_and_branch_from_env(monkeypatch):
    seen = {}

    class Recorder:
        def __init__(self, token, repo, branch):
            seen.update(token=token, repo=repo, branch=branch)

    monkeypatch.setenv("REPAIR_GITHUB_TOKEN", "t0k")
    monkeypatch.delenv("REPAIR_DEMO_REPO", raising=False)
    monkeypatch.delenv("REPAIR_DEMO_BRANCH", raising=False)
    monkeypatch.setattr(injector, "GitOps", Recorder)
    injector._default_git()
    assert seen == {"token": "t0k", "repo": "AkashDeepSinghJassal/repair-demo-app", "branch": "main"}


# --- CLI ---------------------------------------------------------------------------------------


def test_cli_list_prints_ten_faults(capsys):
    assert cli_main(["list"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 10
    assert lines[0].startswith("F1\tgit\tOOM_KILLED")


def test_cli_inject_returns_error_on_fault_error(monkeypatch, capsys):
    monkeypatch.delenv("REPAIR_GITHUB_TOKEN", raising=False)
    assert cli_main(["inject", "F1"]) == 1
    assert "REPAIR_GITHUB_TOKEN" in capsys.readouterr().err


@pytest.mark.parametrize("fault_id", GIT_IDS)
def test_is_injected_distinguishes_healthy_from_broken(fault_id):
    from repair_faults.injector import is_injected

    entry = get_fault(fault_id)
    healthy = _fixture_text(entry["target_file"])
    assert not is_injected(entry, healthy)
    assert is_injected(entry, apply_edits(healthy, edits_for(entry)))
