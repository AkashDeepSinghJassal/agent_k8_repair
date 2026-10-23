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

from . import evidence, git_writer, guardrails, incidents
from .analyzer import InvalidOutput, LlmUnavailable
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


def _bundle_files(name: str, settings: Settings, uid: str | None = None) -> dict[int, Path]:
    base = Path(settings.state_dir).expanduser() / "evidence"
    prefix = f"{name}-{uid}" if uid else name
    found: dict[int, Path] = {}
    for p in base.glob(f"{prefix}-*.json"):
        suffix = p.stem[len(prefix) + 1 :]
        if suffix.isdigit():
            found[int(suffix)] = p
    return found


def load_bundle(name: str, attempts: int, settings: Settings, uid: str | None = None) -> dict | None:
    """Evidence bundle ``<name>[-<uid8>]-<attempts>.json``; falls back to the newest bundle of this incident."""
    found = _bundle_files(name, settings, uid)
    if not found:
        return None
    return json.loads(found.get(attempts, found[max(found)]).read_text())


def refresh_evidence(
    body: dict, attempts: int, settings: Settings, patch: dict, *, collect_fn: Callable[..., dict] | None = None
) -> None:
    """Collect a fresh bundle for ``attempts`` when re-analysing (attempt > 1) and none exists yet.

    Failures are logged and ignored: analysis then uses the newest older bundle.
    """
    name = body["metadata"]["name"]
    if attempts <= 1 or attempts in _bundle_files(name, settings, evidence.uid_tag(body)):
        return
    try:
        if collect_fn is None:
            collect_fn = evidence.collect_and_store
            clients = evidence.default_clients()
        else:
            clients = (None, None, None)
        result = collect_fn(*clients, body, attempt=attempts, state_dir=Path(settings.state_dir))
        patch.setdefault("status", {})["evidenceRef"] = result["evidenceRef"]
    except Exception:
        log.exception("%s: could not collect fresh evidence for attempt %d, using the newest bundle", name, attempts)


def _classification(p: Proposal) -> dict:
    d = {"class": p.cls, "confidence": p.confidence, "source": p.source}
    if p.rule_disagreement:
        d.update(ruleHint=p.rule_disagreement.get("rule"), disagrees=True)
    elif p.rule_disagreement is None and p.source == "llm":
        d["disagrees"] = False
    if p.llm_meta:
        m = p.llm_meta
        d.update(provider=m.get("provider"), model=m.get("model"), latencyMs=m.get("latencyMs"))
        if m.get("usage"):
            d["usage"] = m["usage"]
    return d


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


def _escalate(
    patch: dict, reason: str, now: datetime | None, repo: Any, status: dict,
    incident: dict | None = None, **fields: Any,
) -> None:
    incidents.set_phase(
        patch, Phase.ESCALATED, now, incident=incident, outcome={"result": "escalated", "reason": reason}, **fields
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
    collect_fn: Callable[..., dict] | None = None,
) -> None:
    """Analyzing -> Proposed | Escalated. Retries internally on invalid LLM output or guardrail rejection.

    A re-analysis (attempt > 1) first collects a fresh evidence bundle for that attempt.
    """
    settings = settings or get_settings()
    name = body["metadata"]["name"]
    status = body.get("status") or {}
    attempts = status.get("attempts") or 1
    feedback = list(status.get("reviewFeedback") or [])
    last_reason: str | None = None
    if attempts <= settings.max_attempts:
        refresh_evidence(body, attempts, settings, patch, collect_fn=collect_fn)

    while True:
        if attempts > settings.max_attempts:
            gh = repo
            if gh is None and settings.github_token and status.get("pullRequest"):
                gh = _repo(None, settings)
            _escalate(patch, last_reason or "max_attempts", now, gh, status, incident=body, attempts=attempts, reviewFeedback=feedback)
            _record(store, status, "escalated", settings)
            return
        bundle = load_bundle(name, attempts, settings, evidence.uid_tag(body))
        if bundle is None:
            _escalate(patch, "evidence_missing", now, None, status, incident=body, attempts=attempts)
            return
        try:
            proposal = analyze_fn(bundle, feedback or None, store=store) if store else analyze_fn(bundle, feedback or None)
        except LlmUnavailable as exc:
            log.error("%s: LLM unavailable: %s", name, exc.reason)
            _escalate(patch, f"llm_unavailable: {exc.reason[:160]}", now, None, status, incident=body, attempts=attempts)
            return
        except InvalidOutput as exc:
            last_reason = exc.reason
            log.warning("%s attempt %d: invalid_output", name, attempts)
            attempts += 1
            continue

        common = {"classification": _classification(proposal), "attempts": attempts, "reviewFeedback": feedback}
        if settings.shadow_mode:
            incidents.set_phase(
                patch, Phase.ESCALATED, now, incident=body, shadow=True, proposal=_proposal_status(proposal),
                outcome={"result": "escalated", "reason": "shadow_mode"}, **common,
            )
            return
        if proposal.tier == 4 or not proposal.fixable_in_git:
            incidents.set_phase(
                patch, Phase.ESCALATED, now, incident=body, proposal=_proposal_status(proposal),
                outcome={"result": "escalated", "reason": "not_fixable_in_git"}, **common,
            )
            return

        gh = _repo(repo, settings)
        path = (bundle.get("git") or {}).get("path")
        allowed = evidence.allowed_files(bundle)
        ev_text = evidence.evidence_text(bundle)
        rejected_by, details = None, ""
        if not path or proposal.file not in allowed:
            res = guard_fn(proposal, "", "", path or "", skip=guard_skip, allowed_paths=allowed)
            rejected_by, details = res.rejected_by or "path_allowlist", res.details
        else:
            current = gh.get_contents(proposal.file, ref=gh.default_branch).decoded_content.decode("utf-8")
            try:
                new_text = guardrails.apply_edits(current, proposal.edits)
            except guardrails.GuardrailError as exc:
                rejected_by, details = "exact_edit", str(exc)
            else:
                res = guard_fn(
                    proposal, current, new_text, proposal.file,
                    skip=guard_skip, allowed_paths=allowed, evidence_text=ev_text,
                )
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
            patch, Phase.PROPOSED, now, incident=body, proposal=_proposal_status(proposal),
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
    bundle = load_bundle(name, attempts, settings, evidence.uid_tag(body)) or {}
    gh = _repo(repo, settings)
    current = gh.get_contents(proposal.file, ref=gh.default_branch).decoded_content.decode("utf-8")
    new_text = guardrails.apply_edits(current, proposal.edits)
    pr = git_writer.open_or_update_pr(
        gh, name, proposal, new_text, attempts,
        bundle=bundle, evidence_ref=status.get("evidenceRef"), verify_window=settings.verify_window_seconds,
    )
    incidents.set_phase(patch, Phase.AWAITING_REVIEW, now, incident=body, pullRequest=pr.html_url)


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
    if status.get("phase") not in (None, Phase.AWAITING_REVIEW.value):
        return  # a delayed poll that fires after the phase already moved on must not regress it
    number = _pr_number(status.get("pullRequest"))
    if number is None:
        return
    gh = _repo(repo, settings)
    state = git_writer.poll_pr(gh, number, status.get("reviewCursor"))
    if state.state == "merged":
        _record(store, status, "approved", settings)
        incidents.set_phase(patch, Phase.MERGED, now, incident=body, mergeSha=state.merge_sha)
    elif state.state == "closed":
        _record(store, status, "rejected", settings)
        incidents.set_phase(
            patch, Phase.FAILED, now, incident=body, outcome={"result": "failed", "reason": "rejected_by_reviewer"}
        )
    elif state.state == "changes_requested":
        _record(store, status, "rejected", settings)
        incidents.set_phase(
            patch, Phase.ANALYZING, now, incident=body,
            attempts=(status.get("attempts") or 1) + 1,
            reviewFeedback=[*(status.get("reviewFeedback") or []), *state.feedback],
            reviewCursor=state.cursor,
        )
    elif state.cursor != (status.get("reviewCursor") or {}):
        patch.setdefault("status", {})["reviewCursor"] = state.cursor
