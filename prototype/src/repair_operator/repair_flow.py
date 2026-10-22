"""Collected -> Analyzing -> Proposed -> AwaitingReview -> Merged | Analyzing | Failed (issue #10).

Plain functions that write into a kopf ``patch``; ``main.py`` holds the thin handlers. External
dependencies (analyzer, guardrails, GitHub repo, recipe store) are injectable for tests.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import git_writer, guardrails, incidents
from .analyzer import InvalidOutput
from .analyzer import analyze as default_analyze
from .config import Settings, get_settings
from .models import Phase, Proposal

log = logging.getLogger(__name__)


def _repo(repo: Any, settings: Settings) -> Any:
    if repo is not None:
        return repo
    if not settings.github_token:
        raise RuntimeError("REPAIR_GITHUB_TOKEN is not set")
    return git_writer.repo_client(settings)


def load_bundle(name: str, attempts: int, settings: Settings) -> dict | None:
    """Latest evidence bundle ``<name>-<n>.json`` with n <= attempts (falls back to any)."""
    base = Path(settings.state_dir).expanduser() / "evidence"
    found: dict[int, Path] = {}
    for p in base.glob(f"{name}-*.json"):
        suffix = p.stem[len(name) + 1 :]
        if suffix.isdigit():
            found[int(suffix)] = p
    usable = [n for n in found if n <= max(attempts, 1)] or list(found)
    if not usable:
        return None
    return json.loads(found[max(usable)].read_text())


def _classification(p: Proposal) -> dict:
    return {"class": p.cls, "confidence": p.confidence, "source": p.source}


def _proposal_status(p: Proposal, rejected_by: str | None = None) -> dict:
    d = p.model_dump(by_alias=True)
    d["edits"] = [e.model_dump() for e in p.edits]
    d["rejectedBy"] = rejected_by  # None clears a stale value in the merge patch
    return d


def _record(store: Any, status: dict, outcome: str, settings: Settings) -> None:
    """Teach the recipe store (tier 1/2 proposals only: they carry a template)."""
    prop, sig = status.get("proposal") or {}, status.get("signature")
    if not (sig and prop.get("template")):
        return
    try:
        from . import knowledge

        (store or knowledge.default_store(settings)).record_outcome(
            sig, prop.get("class", ""), prop["template"], prop.get("params") or {}, outcome
        )
    except Exception:
        log.exception("record_outcome(%s) failed", outcome)


def _escalate(patch: dict, reason: str, now: datetime | None, repo: Any, status: dict, **fields: Any) -> None:
    incidents.set_phase(
        patch, Phase.ESCALATED, now, outcome={"result": "escalated", "reason": reason}, **fields
    )
    number = _pr_number(status.get("pullRequest"))
    if number and repo is not None:
        try:
            git_writer.escalate_pr(repo, number, reason)
        except Exception:
            log.exception("could not label/comment on PR #%s", number)


def _pr_number(url: str | None) -> int | None:
    tail = (url or "").rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def analyze_step(
    body: dict,
    patch: dict,
    *,
    repo: Any = None,
    analyze_fn: Callable[..., Proposal] = default_analyze,
    guard_fn: Callable[..., Any] = guardrails.check,
    guard_skip: tuple[str, ...] = (),
    settings: Settings | None = None,
    store: Any = None,
    now: datetime | None = None,
) -> None:
    """Analyzing -> Proposed | Escalated. Retries internally on invalid LLM output or guardrail rejection."""
    settings = settings or get_settings()
    name = body["metadata"]["name"]
    status = body.get("status") or {}
    attempts = status.get("attempts") or 1
    feedback = list(status.get("reviewFeedback") or [])
    last_reason: str | None = None

    while True:
        if attempts > settings.max_attempts:
            gh = repo
            if gh is None and settings.github_token and status.get("pullRequest"):
                gh = _repo(None, settings)
            _escalate(patch, last_reason or "max_attempts", now, gh, status, attempts=attempts, reviewFeedback=feedback)
            _record(store, status, "escalated", settings)
            return
        bundle = load_bundle(name, attempts, settings)
        if bundle is None:
            _escalate(patch, "evidence_missing", now, None, status, attempts=attempts)
            return
        try:
            proposal = analyze_fn(bundle, feedback or None, store=store) if store else analyze_fn(bundle, feedback or None)
        except InvalidOutput as exc:
            last_reason = exc.reason
            log.warning("%s attempt %d: invalid_output", name, attempts)
            attempts += 1
            continue

        common = {"classification": _classification(proposal), "attempts": attempts, "reviewFeedback": feedback}
        if settings.shadow_mode:
            incidents.set_phase(
                patch, Phase.ESCALATED, now, shadow=True, proposal=_proposal_status(proposal),
                outcome={"result": "escalated", "reason": "shadow_mode"}, **common,
            )
            return
        if proposal.tier == 4 or not proposal.fixable_in_git:
            incidents.set_phase(
                patch, Phase.ESCALATED, now, proposal=_proposal_status(proposal),
                outcome={"result": "escalated", "reason": "not_fixable_in_git"}, **common,
            )
            return

        gh = _repo(repo, settings)
        path = (bundle.get("git") or {}).get("path")
        rejected_by, details = None, ""
        if not path or proposal.file != path:
            res = guard_fn(proposal, "", "", path or "", skip=guard_skip)
            rejected_by, details = res.rejected_by or "path_allowlist", res.details
        else:
            current = gh.get_contents(path, ref=gh.default_branch).decoded_content.decode("utf-8")
            try:
                new_text = guardrails.apply_edits(current, proposal.edits)
            except guardrails.GuardrailError as exc:
                rejected_by, details = "exact_edit", str(exc)
            else:
                res = guard_fn(proposal, current, new_text, path, skip=guard_skip)
                if not res.ok:
                    rejected_by, details = res.rejected_by, res.details
        if rejected_by:
            log.warning("%s attempt %d rejected by %s: %s", name, attempts, rejected_by, details)
            last_reason = f"guardrail:{rejected_by}"
            feedback.append(f"Guardrail {rejected_by} rejected the previous proposal: {details}")
            patch.setdefault("status", {})["proposal"] = _proposal_status(proposal, rejected_by)
            attempts += 1
            continue

        incidents.set_phase(
            patch, Phase.PROPOSED, now, proposal=_proposal_status(proposal),
            outcome=None, **common,
        )
        return


def propose_step(
    body: dict,
    patch: dict,
    *,
    repo: Any = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> None:
    """Proposed -> AwaitingReview: commit the edited file to repair/<incident> and open or update the PR."""
    settings = settings or get_settings()
    name = body["metadata"]["name"]
    status = body.get("status") or {}
    attempts = status.get("attempts") or 1
    proposal = Proposal.model_validate(status["proposal"])
    bundle = load_bundle(name, attempts, settings) or {}
    gh = _repo(repo, settings)
    current = gh.get_contents(proposal.file, ref=gh.default_branch).decoded_content.decode("utf-8")
    new_text = guardrails.apply_edits(current, proposal.edits)
    pr = git_writer.open_or_update_pr(
        gh, name, proposal, new_text, attempts,
        bundle=bundle, evidence_ref=status.get("evidenceRef"), verify_window=settings.verify_window_seconds,
    )
    incidents.set_phase(patch, Phase.AWAITING_REVIEW, now, pullRequest=pr.html_url)


def review_step(
    body: dict,
    patch: dict,
    *,
    repo: Any = None,
    settings: Settings | None = None,
    store: Any = None,
    now: datetime | None = None,
) -> None:
    """AwaitingReview poll: merged -> Merged, changes requested -> Analyzing, closed -> Failed."""
    settings = settings or get_settings()
    status = body.get("status") or {}
    number = _pr_number(status.get("pullRequest"))
    if number is None:
        return
    gh = _repo(repo, settings)
    state = git_writer.poll_pr(gh, number, status.get("reviewCursor"))
    if state.state == "merged":
        _record(store, status, "approved", settings)
        incidents.set_phase(patch, Phase.MERGED, now, mergeSha=state.merge_sha)
    elif state.state == "closed":
        _record(store, status, "rejected", settings)
        incidents.set_phase(
            patch, Phase.FAILED, now, outcome={"result": "failed", "reason": "rejected_by_reviewer"}
        )
    elif state.state == "changes_requested":
        _record(store, status, "rejected", settings)
        incidents.set_phase(
            patch, Phase.ANALYZING, now,
            attempts=(status.get("attempts") or 1) + 1,
            reviewFeedback=[*(status.get("reviewFeedback") or []), *state.feedback],
            reviewCursor=state.cursor,
        )
    elif state.cursor != (status.get("reviewCursor") or {}):
        patch.setdefault("status", {})["reviewCursor"] = state.cursor
