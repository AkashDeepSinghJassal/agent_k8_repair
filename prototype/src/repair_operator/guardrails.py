"""Guardrails (CONTRACTS section 8): seven ordered checks between a proposal and GitHub.

``check(proposal, current_text, new_text, path)`` runs path_allowlist, exact_edit, size_cap,
grounded_values, static, server_dry_run and policy in that order and stops at the first failure.

The policy check needs the pre-edit file to compare against (limit raised >4x, registry
change, removed probes, new secrets, new RBAC objects). ``current_text`` is parsed here and
written as ``{"original": [<doc>, ...]}`` into a temp dir that is passed to
``conftest test --data``; the Rego rules read it as ``data.original``.

Every external binary is run with a timeout. A missing binary or a timeout fails closed,
except that kubectl being absent counts as "no cluster" for the optional server dry-run.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

import yaml
from pydantic import BaseModel

from .models import Edit, Proposal

SIZE_CAP = 10
KUBE_CONTEXT = "kind-repair"
CHECK_NAMES = ("path_allowlist", "exact_edit", "size_cap", "static", "server_dry_run", "policy")
DEFAULT_POLICY_DIR = Path(__file__).resolve().parents[2] / "policy"

_ALLOWED = re.compile(r"^(deploy|workflows)/(.+/)?[^/]+\.yaml$")


class GuardrailError(Exception):
    """Raised for a violated guardrail; ``args[0]`` is the check name."""


class GuardrailResult(BaseModel):
    ok: bool
    rejected_by: str | None = None
    details: str = ""


class _MissingBinary(Exception):
    pass


def apply_edits(text: str, edits: Iterable[Edit]) -> str:
    """Apply find/replace edits in order; each ``find`` must occur exactly once."""
    for e in edits:
        if not e.find or text.count(e.find) != 1:
            raise GuardrailError("exact_edit")
        text = text.replace(e.find, e.replace, 1)
    return text


def _run(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    if shutil.which(cmd[0]) is None:
        raise _MissingBinary(f"required binary not found: {cmd[0]}")
    try:
        return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise _MissingBinary(f"{cmd[0]} timed out after {timeout}s") from exc


def _out(p: subprocess.CompletedProcess) -> str:
    return ((p.stdout or "") + (p.stderr or "")).strip()


# ---- checks ------------------------------------------------------------------


def path_allowlist(proposal: Proposal, path: str, allowed: Iterable[str] = ()) -> tuple[bool, str]:
    """``path`` is the file being edited; it must be the proposal's file and in the evidence's allowed set."""
    allowed_set = {a for a in allowed if a} | {path}
    if proposal.file != path or path not in allowed_set:
        return False, f"proposal file {proposal.file!r} is not an evidence file ({sorted(allowed_set)})"
    parts = PurePosixPath(path).parts
    if ".." in parts or path.startswith("/") or "\\" in path or not _ALLOWED.match(path):
        return False, f"{path!r} does not match deploy/**/*.yaml or workflows/**/*.yaml"
    return True, "ok"


def exact_edit(proposal: Proposal, current_text: str, new_text: str) -> tuple[bool, str]:
    if not proposal.edits:
        return False, "proposal has no edits"
    try:
        result = apply_edits(current_text, proposal.edits)
    except GuardrailError:
        return False, "an edit's find string does not occur exactly once in the current file"
    if result != new_text:
        return False, "new_text does not equal current_text with the proposal's edits applied"
    return True, "ok"


def changed_lines(current_text: str, new_text: str) -> int:
    diff = difflib.unified_diff(current_text.splitlines(), new_text.splitlines(), lineterm="", n=0)
    return sum(1 for ln in diff if ln[:1] in "+-" and not ln.startswith(("+++", "---")))


def size_cap(current_text: str, new_text: str, cap: int = SIZE_CAP) -> tuple[bool, str]:
    n = changed_lines(current_text, new_text)
    return (n <= cap), f"{n} changed lines (cap {cap})"


_SCHEME_RE = re.compile(r"[A-Za-z][\w+.-]*://[^\s\"'`,<>]+")
_HOSTPORT_RE = re.compile(r"(?<![\w./:-])[A-Za-z0-9][\w.-]*:\d{2,5}\b")
_UNIT_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?:Ki|Mi|Gi|Ti)\b")
_QUOTED_RE = re.compile(r"\"([^\"]{8,})\"|'([^']{8,})'")
_IMAGE_RE = re.compile(r"^\s*-?\s*image:\s*[\"']?([^\s\"']+)")


def added_literals(current_text: str, new_text: str) -> list[str]:
    """Literals on lines the edits added: URLs, host:port, image refs, sizes, quoted strings with ':' or '/'."""
    old = set(current_text.splitlines())
    out: dict[str, None] = {}
    for line in new_text.splitlines():
        if line in old or not line.strip():
            continue
        for m in _SCHEME_RE.finditer(line):
            out[m.group(0)] = None
        for m in _HOSTPORT_RE.finditer(line):
            out[m.group(0)] = None
        for m in _UNIT_RE.finditer(line):
            out[m.group(0)] = None
        for m in _QUOTED_RE.finditer(line):
            val = m.group(1) or m.group(2)
            if ":" in val or "/" in val:
                out[val] = None
        m = _IMAGE_RE.match(line)
        if m and ("/" in m.group(1) or ":" in m.group(1)):
            out[m.group(1)] = None
    return list(out)


def grounded_values(proposal: Proposal, current_text: str, new_text: str, evidence_text: str) -> tuple[bool, str]:
    """Every literal added by the edits must appear verbatim in the evidence or the original file."""
    if proposal.template:  # template-computed values (new_limit clamps, tags from the diff) are exempt
        return True, "template edit (exempt)"
    for lit in added_literals(current_text, new_text):
        if lit not in evidence_text and lit not in current_text:
            return False, f"ungrounded value {lit!r}: it appears nowhere in the evidence or the original file"
    return True, "ok"


def _kinds(text: str) -> set[str]:
    try:
        return {d.get("kind") for d in yaml.safe_load_all(text) if isinstance(d, dict)}
    except yaml.YAMLError:
        return set()


def static(new_text: str, path: str) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / (Path(path).name or "manifest.yaml")
        f.write_text(new_text)
        notes: list[str] = []
        try:
            p = _run(["yamllint", "-d", "relaxed", str(f)])
            if p.returncode != 0:
                return False, f"yamllint: {_out(p)}"
            notes.append("yamllint ok")
            p = _run(["kubeconform", "-strict", "-ignore-missing-schemas", "-summary", str(f)])
            if p.returncode != 0:
                return False, f"kubeconform: {_out(p)}"
            notes.append("kubeconform ok")
            if "WorkflowTemplate" in _kinds(new_text):
                if shutil.which("argo") is None:
                    notes.append("argo lint skipped: argo not installed")
                else:
                    p = _run(["argo", "lint", "--offline", str(f)])
                    if p.returncode != 0:
                        return False, f"argo lint: {_out(p)}"
                    notes.append("argo lint ok")
        except _MissingBinary as exc:
            return False, str(exc)
    return True, "; ".join(notes)


def cluster_reachable(context: str = KUBE_CONTEXT) -> bool:
    try:
        p = _run(["kubectl", "--context", context, "get", "--raw", "/readyz"], timeout=5)
    except _MissingBinary:
        return False
    return p.returncode == 0


def server_dry_run(new_text: str, path: str, context: str = KUBE_CONTEXT) -> tuple[bool, str]:
    if not cluster_reachable(context):
        return True, "skipped: no cluster"
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / (Path(path).name or "manifest.yaml")
        f.write_text(new_text)
        try:
            p = _run(["kubectl", "--context", context, "apply", "--dry-run=server", "-f", str(f)], timeout=60)
        except _MissingBinary as exc:
            return False, str(exc)
    return (p.returncode == 0), _out(p)


def policy(current_text: str, new_text: str, path: str, policy_dir: Path | str | None = None) -> tuple[bool, str]:
    pdir = Path(policy_dir or os.environ.get("REPAIR_POLICY_DIR") or DEFAULT_POLICY_DIR)
    if not pdir.is_dir():
        return False, f"policy directory not found: {pdir}"
    try:
        original = [d for d in yaml.safe_load_all(current_text) if isinstance(d, dict)]
    except yaml.YAMLError:
        original = []
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "data"
        data.mkdir()
        (data / "original.json").write_text(json.dumps({"original": original}, default=str))
        f = Path(tmp) / (Path(path).name or "manifest.yaml")
        f.write_text(new_text)
        try:
            p = _run(["conftest", "test", "--no-color", "--policy", str(pdir), "--data", str(data), str(f)])
        except _MissingBinary as exc:
            return False, str(exc)
    return (p.returncode == 0), _out(p)


def check(
    proposal: Proposal,
    current_text: str,
    new_text: str,
    path: str,
    *,
    skip: Iterable[str] = (),
    policy_dir: Path | str | None = None,
    allowed_paths: Iterable[str] = (),
    evidence_text: str | None = None,
) -> GuardrailResult:
    """Run the seven checks in order; the first failure wins. ``skip`` names checks to skip (tests).

    ``evidence_text=None`` skips ``grounded_values`` (callers without a bundle)."""
    skipped = set(skip)
    steps: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
        ("path_allowlist", lambda: path_allowlist(proposal, path, allowed_paths)),
        ("exact_edit", lambda: exact_edit(proposal, current_text, new_text)),
        ("size_cap", lambda: size_cap(current_text, new_text)),
        ("grounded_values", lambda: grounded_values(proposal, current_text, new_text, evidence_text)
         if evidence_text is not None else (True, "no evidence given")),
        ("static", lambda: static(new_text, path)),
        ("server_dry_run", lambda: server_dry_run(new_text, path)),
        ("policy", lambda: policy(current_text, new_text, path, policy_dir)),
    ]
    details: list[str] = []
    for name, fn in steps:
        if name in skipped:
            continue
        try:
            ok, msg = fn()
        except Exception as exc:  # noqa: BLE001 - fail closed on any unexpected error
            ok, msg = False, f"{type(exc).__name__}: {exc}"
        if not ok:
            return GuardrailResult(ok=False, rejected_by=name, details=msg)
        details.append(f"{name}: {msg}")
    return GuardrailResult(ok=True, rejected_by=None, details="; ".join(details))
