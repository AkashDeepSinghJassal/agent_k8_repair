"""PR writer and review reader for the demo-app repo (issue #10).

``repo`` is a PyGithub ``Repository`` (or a fake with the same methods). All bot comments carry
``BOT_MARKER`` so the poller can ignore them even though the PAT owner is also the human reviewer.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .models import Proposal

log = logging.getLogger(__name__)

BOT_MARKER = "<!-- repair-operator -->"
RETRY_COMMAND = "/repair retry"
LABEL_AI = "ai-repair"
LABEL_NEEDS_HUMAN = "needs-human"


def branch_name(incident: str) -> str:
    return f"repair/{incident}"


def summary(proposal: Proposal, limit: int = 60) -> str:
    text = (proposal.root_cause or proposal.rationale or proposal.cls).strip().split("\n")[0].rstrip(".")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_body(
    incident: str,
    proposal: Proposal,
    bundle: dict | None = None,
    evidence_ref: str | None = None,
    verify_window: int = 120,
) -> str:
    b = bundle or {}
    src, fail = b.get("source") or {}, b.get("failure") or {}
    stamps = sorted(e.get("lastTimestamp") or e.get("firstTimestamp") or "" for e in b.get("events") or [])
    stamps = [s for s in stamps if s]
    decided = {"rules": "rules", "recipe": "a promoted recipe"}.get(proposal.source, "the LLM")
    return "\n".join(
        [
            BOT_MARKER,
            "## What failed",
            f"- Workload: `{src.get('owner') or src.get('name') or 'unknown'}` (container `{src.get('container') or '-'}`)",
            f"- Reason: `{fail.get('reason') or 'unknown'}`, exit code {fail.get('exitCode')}, restarts {fail.get('restartCount', 0)}",
            f"- First / last seen: {stamps[0] if stamps else 'n/a'} / {stamps[-1] if stamps else 'n/a'}",
            "",
            "## Diagnosis",
            f"- Class: `{proposal.cls}` (confidence {proposal.confidence:.2f}), decided by {decided} (tier {proposal.tier})",
            f"- Root cause: {proposal.root_cause}",
            "",
            "## Change",
            f"- File: `{proposal.file}`",
            f"- Rationale: {proposal.rationale}",
            "",
            "## How it will be verified",
            f"- {proposal.verify or 'Failed run re-submitted / pods stay Ready'} within a {verify_window}s verify window",
            "",
            "## Evidence",
            f"- Incident: `{incident}`",
            f"- Evidence: `{evidence_ref or 'n/a'}`",
            "",
            f"_Request changes with a review, or comment `{RETRY_COMMAND} <feedback>` to have the operator try again._",
        ]
    )


def _find_pr(repo: Any, branch: str, state: str = "open") -> Any | None:
    for pr in repo.get_pulls(state=state, head=f"{repo.owner.login}:{branch}"):
        return pr
    return None


def open_or_update_pr(
    repo: Any,
    incident: str,
    proposal: Proposal,
    new_content: str,
    attempt: int,
    *,
    bundle: dict | None = None,
    evidence_ref: str | None = None,
    verify_window: int = 120,
) -> Any:
    """Create ``repair/<incident>`` (if missing), commit ``new_content`` to it and open the PR (if missing).

    Idempotent: a retried handler reuses the branch and the PR. On later attempts the PR body is refreshed
    and a bot comment records the new attempt.
    """
    branch = branch_name(incident)
    try:
        repo.get_branch(branch)
        # The incident name is deterministic: a branch whose PR was already merged or closed belongs to an
        # earlier incident with the same fingerprint. Start over from the default branch instead of reusing it.
        if _find_pr(repo, branch) is None and _find_pr(repo, branch, "closed") is not None:
            repo.get_git_ref(f"heads/{branch}").delete()
            raise LookupError(branch)
    except Exception:  # noqa: BLE001 - 404 (or just deleted) -> create from the default branch
        repo.create_git_ref(ref=f"refs/heads/{branch}", sha=repo.get_branch(repo.default_branch).commit.sha)
    current = repo.get_contents(proposal.file, ref=branch)
    if current.decoded_content.decode("utf-8") != new_content:
        repo.update_file(
            proposal.file,
            f"repair({proposal.cls}): {summary(proposal)}\n\nRepair-Incident: {incident}\nAttempt: {attempt}",
            new_content,
            current.sha,
            branch=branch,
        )
    body = render_body(incident, proposal, bundle, evidence_ref, verify_window)
    pr = _find_pr(repo, branch)
    if pr is None:
        pr = repo.create_pull(
            title=f"[repair] {proposal.cls}: {summary(proposal)}",
            body=body,
            head=branch,
            base=repo.default_branch,
            draft=False,
        )
        pr.add_to_labels(LABEL_AI, proposal.cls.lower())
    else:
        pr.edit(body=body)
        pr.create_issue_comment(f"{BOT_MARKER}\nAttempt {attempt}: pushed a new proposal ({proposal.cls}, tier {proposal.tier}).")
    return pr


@dataclass
class ReviewState:
    state: str  # open | changes_requested | merged | closed
    merge_sha: str | None = None
    feedback: list[str] = field(default_factory=list)
    cursor: dict = field(default_factory=dict)


def poll_pr(repo: Any, number: int, cursor: dict | None = None, settle: float = 3.0) -> ReviewState:
    """Read the PR. ``cursor`` is ``{"review": id, "comment": id}``: feedback at or below it was already handled.

    Merged wins over everything. Changes requested = a new CHANGES_REQUESTED review, or a new human
    comment starting with ``/repair retry`` (the PAT owner cannot review their own PR).
    """
    cur = {"review": 0, "comment": 0, **(cursor or {})}
    pr = repo.get_pull(number)
    if pr.merged:
        return ReviewState("merged", merge_sha=pr.merge_commit_sha, cursor=cur)
    if pr.state == "closed":
        # GitHub can report closed before the merged flag is visible right after a merge: look again.
        time.sleep(settle)
        pr = repo.get_pull(number)
        if pr.merged:
            return ReviewState("merged", merge_sha=pr.merge_commit_sha, cursor=cur)
        return ReviewState("closed", cursor=cur)
    feedback: list[str] = []
    new = dict(cur)
    line_comments = list(pr.get_review_comments())
    for rv in pr.get_reviews():
        if rv.id <= cur["review"]:
            continue
        new["review"] = max(new["review"], rv.id)
        if rv.state != "CHANGES_REQUESTED":
            continue
        parts = [(rv.body or "").strip()]
        parts += [
            f"{c.path}:{getattr(c, 'line', None) or '?'}: {c.body}"
            for c in line_comments
            if getattr(c, "pull_request_review_id", None) == rv.id
        ]
        feedback.append("\n".join(p for p in parts if p) or "changes requested")
    for c in pr.get_issue_comments():
        if c.id <= cur["comment"]:
            continue
        new["comment"] = max(new["comment"], c.id)
        text = (c.body or "").strip()
        if BOT_MARKER in text or not text.lower().startswith(RETRY_COMMAND):
            continue
        feedback.append(text[len(RETRY_COMMAND):].strip() or "retry requested")
    return ReviewState("changes_requested" if feedback else "open", feedback=feedback, cursor=new)


def escalate_pr(repo: Any, number: int, reason: str) -> None:
    """Label ``needs-human`` and comment once."""
    pr = repo.get_pull(number)
    if any(lb.name == LABEL_NEEDS_HUMAN for lb in pr.get_labels()):
        return
    pr.add_to_labels(LABEL_NEEDS_HUMAN)
    pr.create_issue_comment(f"{BOT_MARKER}\nEscalating to a human: {reason}.")


def repo_client(settings: Any) -> Any:
    from github import Auth, Github

    return Github(auth=Auth.Token(settings.github_token)).get_repo(settings.demo_repo)
