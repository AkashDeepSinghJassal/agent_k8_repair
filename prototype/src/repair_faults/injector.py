"""Fault catalog loader and injector for the demo app.

Git faults commit inverse-able edits to the demo repo through a GitHub client. Cluster faults run
kubectl actions through an injectable runner. This module must not import repair_operator.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

CATALOG_PATH = Path(__file__).with_name("catalog.yaml")
DEFAULT_DEMO_REPO = "AkashDeepSinghJassal/repair-demo-app"
DEFAULT_DEMO_BRANCH = "main"
DEFAULT_KUBE_CONTEXT = "kind-repair"
DEFAULT_WORKFLOW_TEMPLATE = "release-pipeline"

TAXONOMY = {"OOM_KILLED", "IMAGE_PULL", "CONFIG_ERROR", "CRASH_LOOP", "STAGE_FAILURE", "UNSCHEDULABLE"}
OUTCOMES = {"Healed", "Escalated", "Blocked"}
KINDS = {"git", "workflow", "cluster"}
REQUIRED_KEYS = {
    "id",
    "title",
    "description",
    "namespace",
    "target_file",
    "kind",
    "breaking_edits",
    "expected_class",
    "expected_outcome",
    "correct_fix",
}
# Optional keys: commit_message (neutral inject commit title; default chore(<dir>): update <file>), restart_target (deployment to restart after Argo syncs), evidence_milestones (2-4 facts a
# correct investigation must establish), post_inject_commands / post_revert_commands (kubectl argv lists run
# after the commit of a git fault, e.g. noise), submit_workflow + argo_app (create a release-pipeline run once
# Argo CD has synced the injected commit (submit_delay_seconds waits longer, e.g. for terminating pods to leave the Service), for faults whose symptom is a failing workflow step).
SYNC_WAIT_SECONDS = 150
ARGO_APP_BY_NAMESPACE = {"demo": "demo-app", "demo-b": "demo-app-b"}
_sleep = time.sleep


class FaultError(Exception):
    """Raised for an invalid catalog, an unknown fault, or an edit that does not match exactly once."""


@dataclass(frozen=True)
class Edit:
    find: str
    replace: str

    def inverse(self) -> Edit:
        return Edit(find=self.replace, replace=self.find)


class GitClient(Protocol):
    def get_file(self, path: str) -> str: ...

    def commit_file(self, path: str, text: str, message: str) -> str: ...


class KubectlRunner(Protocol):
    def __call__(self, args: list[str], stdin: str | None = None) -> str: ...


class GitOps:
    """GitHub client for the demo repo (PyGithub). Commits go to the configured branch."""

    def __init__(self, token: str, repo: str = DEFAULT_DEMO_REPO, branch: str = DEFAULT_DEMO_BRANCH):
        from github import Auth, Github

        self._repo = Github(auth=Auth.Token(token)).get_repo(repo)
        self._branch = branch

    def get_file(self, path: str) -> str:
        return self._repo.get_contents(path, ref=self._branch).decoded_content.decode("utf-8")

    def commit_file(self, path: str, text: str, message: str) -> str:
        current = self._repo.get_contents(path, ref=self._branch)
        result = self._repo.update_file(path, message, text, current.sha, branch=self._branch)
        return result["commit"].sha


class Kubectl:
    """Default kubectl runner. Runs `kubectl --context <ctx> ...` and returns stdout."""

    def __init__(self, context: str = DEFAULT_KUBE_CONTEXT):
        self._context = context

    def __call__(self, args: list[str], stdin: str | None = None) -> str:
        cmd = ["kubectl", "--context", self._context, *args]
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise FaultError(f"kubectl {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout


def load_catalog(path: Path = CATALOG_PATH) -> list[dict[str, Any]]:
    """Load and validate the catalog. Raises FaultError on any problem."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("faults")
    if not isinstance(entries, list) or not entries:
        raise FaultError(f"{path}: expected a non-empty 'faults' list")
    seen: set[str] = set()
    for entry in entries:
        _validate_entry(entry)
        if entry["id"] in seen:
            raise FaultError(f"duplicate fault id {entry['id']}")
        seen.add(entry["id"])
    return entries


def _validate_entry(entry: dict[str, Any]) -> None:
    fid = entry.get("id", "?")
    missing = REQUIRED_KEYS - set(entry)
    if missing:
        raise FaultError(f"{fid}: missing keys {sorted(missing)}")
    if entry["kind"] not in KINDS:
        raise FaultError(f"{fid}: unknown kind {entry['kind']!r}")
    if entry["expected_class"] not in TAXONOMY:
        raise FaultError(f"{fid}: unknown expected_class {entry['expected_class']!r}")
    if entry["expected_outcome"] not in OUTCOMES:
        raise FaultError(f"{fid}: unknown expected_outcome {entry['expected_outcome']!r}")
    _validate_optional(entry)
    if entry["kind"] == "cluster":
        if entry["target_file"] is not None:
            raise FaultError(f"{fid}: cluster faults must have target_file null")
        if not entry.get("inject_commands") or not entry.get("revert_commands"):
            raise FaultError(f"{fid}: cluster faults need inject_commands and revert_commands")
    else:
        if not entry["target_file"] or not entry["breaking_edits"]:
            raise FaultError(f"{fid}: git faults need target_file and breaking_edits")
        for edit in entry["breaking_edits"]:
            if set(edit) != {"find", "replace"} or not edit["find"]:
                raise FaultError(f"{fid}: each breaking edit needs non-empty find and replace")


def _validate_optional(entry: dict[str, Any]) -> None:
    fid = entry["id"]
    target = entry.get("restart_target")
    if target is not None and not (isinstance(target, str) and target):
        raise FaultError(f"{fid}: restart_target must be a non-empty deployment name")
    milestones = entry.get("evidence_milestones")
    if milestones is not None and not (
        isinstance(milestones, list) and 2 <= len(milestones) <= 4 and all(isinstance(m, str) and m for m in milestones)
    ):
        raise FaultError(f"{fid}: evidence_milestones must be a list of 2-4 non-empty strings")
    for key in ("post_inject_commands", "post_revert_commands"):
        cmds = entry.get(key)
        if cmds is not None and not (isinstance(cmds, list) and all(isinstance(c, list) and c for c in cmds)):
            raise FaultError(f"{fid}: {key} must be a list of kubectl argv lists")
    message = entry.get("commit_message")
    if message is not None and not (isinstance(message, str) and message.strip()):
        raise FaultError(f"{fid}: commit_message must be a non-empty string")
    delay = entry.get("submit_delay_seconds", 0)
    if not isinstance(delay, int) or delay < 0:
        raise FaultError(f"{fid}: submit_delay_seconds must be a non-negative integer")
    if entry.get("submit_workflow") and not entry.get("argo_app"):
        raise FaultError(f"{fid}: submit_workflow needs argo_app")


def list_faults(path: Path = CATALOG_PATH) -> list[dict[str, Any]]:
    return load_catalog(path)


def get_fault(fault_id: str, path: Path = CATALOG_PATH) -> dict[str, Any]:
    for entry in load_catalog(path):
        if entry["id"] == fault_id:
            return entry
    raise FaultError(f"unknown fault {fault_id!r}")


def edits_for(entry: dict[str, Any]) -> list[Edit]:
    return [Edit(find=e["find"], replace=e["replace"]) for e in entry["breaking_edits"]]


def apply_edits(text: str, edits: list[Edit]) -> str:
    """Apply edits in order. Each `find` must occur exactly once in the text at the time it is applied."""
    for edit in edits:
        count = text.count(edit.find)
        if count != 1:
            raise FaultError(f"find text occurs {count} times (expected 1): {edit.find[:60]!r}")
        text = text.replace(edit.find, edit.replace, 1)
    return text


def inverse_edits(edits: list[Edit]) -> list[Edit]:
    """Inverse of a sequence of edits, in reverse order."""
    return [e.inverse() for e in reversed(edits)]


def is_injected(entry: dict[str, Any], text: str) -> bool:
    """True when ``text`` carries the fault (every breaking edit's replacement is present).

    Plain "does the inverse apply" is ambiguous when one side is a substring of the other (F3, F4, F5), so an
    edit counts as applied when its replacement is present and either the original text is gone or the original
    is itself contained in the replacement.
    """
    return all(
        e.replace in text and (e.find not in text or e.find in e.replace) for e in edits_for(entry)
    )


def _commit_message(entry: dict[str, Any], revert: bool) -> str:
    if revert:
        return f"fault({entry['id']}): revert {entry['title']}\n\nRepair-Fault-Revert: {entry['id']}"
    # Neutral on purpose: the agent can read commits with git_log, so the message must not name the fault.
    if entry.get("commit_message"):
        return entry["commit_message"]
    target = entry["target_file"]
    return f"chore({os.path.basename(os.path.dirname(target)) or 'repo'}): update {os.path.basename(target)}"


def _wait_synced(kube: KubectlRunner, app: str, sha: str) -> bool:
    """Hard-refresh the Argo CD app and wait until it is Synced at ``sha``. False on timeout (best effort)."""
    kube(["-n", "argocd", "annotate", "application", app, "argocd.argoproj.io/refresh=hard", "--overwrite"])
    for _ in range(SYNC_WAIT_SECONDS // 5):
        out = kube(["-n", "argocd", "get", "application", app, "-o", "jsonpath={.status.sync.revision} {.status.sync.status}"])
        parts = out.split()
        if len(parts) == 2 and sha.startswith(parts[0][:7]) and parts[1] == "Synced":
            return True
        _sleep(5)
    return False


def _run_post(entry: dict[str, Any], key: str, kube: KubectlRunner | None, result: dict[str, Any]) -> KubectlRunner | None:
    for argv in entry.get(key) or []:
        kube = kube or Kubectl()
        kube(list(argv))
        result["actions"].append(["kubectl", *argv])
    return kube


def _workflow_run_manifest(namespace: str) -> str:
    return (
        "apiVersion: argoproj.io/v1alpha1\n"
        "kind: Workflow\n"
        "metadata:\n"
        "  generateName: release-pipeline-fault-\n"
        f"  namespace: {namespace}\n"
        "spec:\n"
        "  workflowTemplateRef:\n"
        f"    name: {DEFAULT_WORKFLOW_TEMPLATE}\n"
    )


def inject(
    fault_id: str,
    *,
    git: GitClient | None = None,
    kubectl: KubectlRunner | None = None,
    catalog_path: Path = CATALOG_PATH,
) -> dict[str, Any]:
    """Inject a fault. Git faults commit the breaking edits; cluster faults run inject_commands."""
    entry = get_fault(fault_id, catalog_path)
    result: dict[str, Any] = {"id": fault_id, "kind": entry["kind"], "actions": []}
    if entry["kind"] == "cluster":
        kube = kubectl or Kubectl()
        for argv in entry["inject_commands"]:
            kube(list(argv))
            result["actions"].append(["kubectl", *argv])
        return result

    git = git or _default_git()
    path = entry["target_file"]
    new_text = apply_edits(git.get_file(path), edits_for(entry))
    result["commit"] = git.commit_file(path, new_text, _commit_message(entry, revert=False))
    result["actions"].append(["commit", path])
    if entry["kind"] == "workflow":
        kube = kubectl or Kubectl()
        kube(["-n", entry["namespace"], "apply", "-f", "-"], stdin=new_text)
        kube(["-n", entry["namespace"], "create", "-f", "-"], stdin=_workflow_run_manifest(entry["namespace"]))
        result["actions"].append(["kubectl apply template", DEFAULT_WORKFLOW_TEMPLATE])
        result["actions"].append(["kubectl create workflow run", entry["namespace"]])
    kube = _run_post(entry, "post_inject_commands", kubectl, result)
    if entry["kind"] == "git" and entry.get("submit_workflow"):
        kube = kube or Kubectl()
        result["synced"] = _wait_synced(kube, entry["argo_app"], result["commit"])
        _sleep(int(entry.get("submit_delay_seconds", 0)))
        kube(["-n", entry["namespace"], "create", "-f", "-"], stdin=_workflow_run_manifest(entry["namespace"]))
        result["actions"].append(["kubectl create workflow run", entry["namespace"]])
    target = entry.get("restart_target")
    if entry["kind"] == "git" and target:  # ConfigMap-only fault: a running pod never sees the change until restarted
        kube = kube or Kubectl()
        app = entry.get("argo_app") or ARGO_APP_BY_NAMESPACE.get(entry["namespace"], "demo-app")
        result["synced"] = _wait_synced(kube, app, result["commit"])
        kube(["-n", entry["namespace"], "rollout", "restart", f"deploy/{target}"])
        result["actions"].append(["kubectl rollout restart", f"deploy/{target}"])
    return result


def revert(
    fault_id: str,
    *,
    git: GitClient | None = None,
    kubectl: KubectlRunner | None = None,
    catalog_path: Path = CATALOG_PATH,
) -> dict[str, Any]:
    """Revert a fault: inverse edits for git faults, revert_commands for cluster faults."""
    entry = get_fault(fault_id, catalog_path)
    result: dict[str, Any] = {"id": fault_id, "kind": entry["kind"], "actions": []}
    if entry["kind"] == "cluster":
        kube = kubectl or Kubectl()
        for argv in entry["revert_commands"]:
            kube(list(argv))
            result["actions"].append(["kubectl", *argv])
        return result

    git = git or _default_git()
    path = entry["target_file"]
    new_text = apply_edits(git.get_file(path), inverse_edits(edits_for(entry)))
    result["commit"] = git.commit_file(path, new_text, _commit_message(entry, revert=True))
    result["actions"].append(["commit", path])
    if entry["kind"] == "workflow":
        kube = kubectl or Kubectl()
        kube(["-n", entry["namespace"], "apply", "-f", "-"], stdin=new_text)
        result["actions"].append(["kubectl apply template", DEFAULT_WORKFLOW_TEMPLATE])
    _run_post(entry, "post_revert_commands", kubectl, result)
    return result


def _default_git() -> GitOps:
    token = os.environ.get("REPAIR_GITHUB_TOKEN")
    if not token:
        raise FaultError("REPAIR_GITHUB_TOKEN is not set")
    return GitOps(
        token=token,
        repo=os.environ.get("REPAIR_DEMO_REPO", DEFAULT_DEMO_REPO),
        branch=os.environ.get("REPAIR_DEMO_BRANCH", DEFAULT_DEMO_BRANCH),
    )

