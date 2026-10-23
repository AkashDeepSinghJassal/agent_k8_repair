"""Kopf handlers for Merged -> Verifying -> Healed | Analyzing | Escalated, plus the learner.

Status fields read:  ``mergeSha`` (or ``mergedSha``) set by the PR poller (#10), ``pullRequest``,
``proposal`` (file, edits, template, params), ``signature``, ``attempts``, ``reviewFeedback``,
``timestamps.merged``, ``spec.source``.
Status fields written: ``verify`` {startedAt, healthySince, rerun, restarts}, ``reviewFeedback``, ``outcome``,
``learning`` {recorded, outcome, mergedMatchesProposal}.

Plain functions hold the logic; the Kopf handlers at the bottom are thin wrappers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import kopf

from . import incidents, knowledge, retrigger
from .config import get_settings
from .incidents import GROUP, PLURAL, VERSION
from .models import TERMINAL_PHASES, Phase

log = logging.getLogger(__name__)

SYNC_POLL_SECONDS = 5


def merge_sha(status: dict) -> str | None:
    return status.get("mergeSha") or status.get("mergedSha")


# --- Merged -> Verifying ---------------------------------------------------------


def merged_step(body: dict, patch: dict, custom_api: Any, now: datetime | None = None) -> float:
    """Wait for Argo CD to sync the merge SHA, re-run, enter Verifying. Returns the delay if still waiting."""
    now = now or datetime.now(UTC)
    status = body.get("status") or {}
    ns = body["metadata"]["namespace"]
    sha = merge_sha(status)
    if not sha:
        _close(patch, Phase.FAILED, "missing_merge_sha", now, body)
        return 0.0
    try:
        wf = retrigger.is_workflow(body)
        retrigger.refresh_app(custom_api, ns, wf)
        app = retrigger.get_app(custom_api, ns, wf)
    except Exception as exc:  # noqa: BLE001  (Argo CD unreachable: keep trying until the timeout)
        log.warning("argo cd lookup failed: %s", exc)
        app = {}
    if not retrigger.is_synced(app, sha):
        if retrigger.sync_timed_out((status.get("timestamps") or {}).get("merged"), now):
            _close(patch, Phase.FAILED, "sync_timeout", now, body)
            return 0.0
        return float(SYNC_POLL_SECONDS)
    verify: dict[str, Any] = {"startedAt": retrigger.iso(now), "healthySince": None}
    rerun = retrigger.retrigger(body, custom_api=custom_api)
    if rerun:
        verify["rerun"] = rerun["rerun"]["name"]
    incidents.set_phase(patch, Phase.VERIFYING, now, incident=body, verify=verify)
    return 0.0


# --- Verifying -> Healed | Analyzing | Escalated ---------------------------------


def _close(
    patch: dict, phase: Phase, reason: str, now: datetime | None = None, incident: dict | None = None
) -> None:
    result = {Phase.FAILED: "failed", Phase.ESCALATED: "escalated", Phase.HEALED: "healed"}[phase]
    incidents.set_phase(patch, phase, now, incident=incident, outcome={"result": result, "reason": reason})


def verify_step(
    body: dict, patch: dict, core_api: Any, custom_api: Any, window: int | None = None,
    max_attempts: int | None = None, now: datetime | None = None,
) -> str:
    """One timer tick. Returns the verdict state (waiting|healthy|healed|failed)."""
    settings = get_settings()
    window = settings.verify_window_seconds if window is None else window
    max_attempts = settings.max_attempts if max_attempts is None else max_attempts
    now = now or datetime.now(UTC)
    verdict = retrigger.evaluate(body, core_api=core_api, custom_api=custom_api, window=window, now=now)
    status = body.get("status") or {}
    verify = status.get("verify") or {}
    if verdict.state in ("healthy", "waiting"):  # persist the clock and the restart baselines
        out: dict[str, Any] = {}
        if verdict.healthy_since != verify.get("healthySince"):
            out["healthySince"] = verdict.healthy_since  # None resets the window
        if verdict.restarts is not None:
            old = verify.get("restarts") or {}
            merged = {**{k: None for k in old if k not in verdict.restarts}, **verdict.restarts}
            if merged != old:
                out["restarts"] = merged
        if out:
            patch.setdefault("status", {}).setdefault("verify", {}).update(out)
    elif verdict.state == "healed":
        _close(patch, Phase.HEALED, verdict.reason, now, body)
    elif verdict.state == "failed":
        feedback = list(status.get("reviewFeedback") or [])
        feedback.append("re-run failed: " + "; ".join([verdict.reason, *verdict.details]))
        attempts = (status.get("attempts") or 0) + 1  # this failed verification consumed an attempt
        if attempts > max_attempts:  # same comparison as repair_flow.analyze_step
            incidents.set_phase(
                patch, Phase.ESCALATED, now, incident=body, reviewFeedback=feedback, verify=None, attempts=attempts,
                outcome={"result": "escalated", "reason": f"attempts exhausted: {verdict.reason}"},
            )
        else:
            incidents.set_phase(
                patch, Phase.ANALYZING, now, incident=body, reviewFeedback=feedback, verify=None, attempts=attempts
            )
    return verdict.state


# --- Learner ----------------------------------------------------------------------


def fetch_merged_file(repo: Any, path: str, sha: str) -> str:
    """Content of ``path`` at the merge commit (PyGithub ``repo.get_contents``)."""
    item = repo.get_contents(path, ref=sha)
    if isinstance(item, list):
        raise TypeError(f"{path} is a directory")
    return item.decoded_content.decode("utf-8")


def edits_applied(merged_text: str, edits: list[dict]) -> bool:
    """True when the merged file carries every proposed edit (reviewer-only changes elsewhere are fine)."""
    for e in edits:
        find, replace = e.get("find", ""), e.get("replace", "")
        if replace not in merged_text:
            return False
        if find and find not in replace and find in merged_text:
            return False
    return True


def learn(
    body: dict, patch: dict, *, repo: Any = None, fetch: Callable[[Any, str, str], str] = fetch_merged_file,
    store: Any = None,
) -> str | None:
    """Record the outcome in the recipe store. Returns the outcome recorded, or None.

    Healed + merged file equals the proposal -> ``healed`` (counts toward promotion).
    Healed but the reviewer changed the proposal -> ``approved`` only (no promotion credit).
    Failed / Escalated after a merge -> ``failed`` (demotes a promoted recipe).
    """
    status = body.get("status") or {}
    if status.get("learning"):
        return None  # already learned
    prop = status.get("proposal") or {}
    sig = status.get("signature") or (body["metadata"].get("annotations") or {}).get(f"{GROUP}/signature")
    cls = (status.get("classification") or {}).get("class") or prop.get("class") or ""
    if not sig or not prop.get("template") or not merge_sha(status):
        return None
    phase = status.get("phase")
    matches: bool | None = None
    if phase == Phase.HEALED.value:
        try:
            if repo is None or not prop.get("file"):
                raise LookupError("no repo or file")
            matches = edits_applied(fetch(repo, prop["file"], merge_sha(status)), prop.get("edits") or [])
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot compare merged file: %s", exc)
            matches = None
        outcome = "healed" if matches else "approved"
    else:
        outcome = "failed"
    (store or knowledge.default_store()).record_outcome(sig, cls, prop["template"], prop.get("params"), outcome)
    patch.setdefault("status", {})["learning"] = {
        "recorded": True, "outcome": outcome, "mergedMatchesProposal": matches,
    }
    return outcome


def closed_step(body: dict, patch: dict, repo: Any = None) -> None:
    """Terminal phase reached: start the cooldown and learn from the outcome."""
    spec_fp = (body.get("spec") or {}).get("fingerprint")
    if spec_fp:
        incidents.start_cooldown(spec_fp)
    learn(body, patch, repo=repo)


# --- cluster clients (lazy) and Kopf wrappers -------------------------------------


def _clients() -> tuple[Any, Any, Any]:
    from .evidence import default_clients

    return default_clients()


@kopf.on.field(GROUP, VERSION, PLURAL, field="status.phase", value=Phase.MERGED.value)
def on_merged_sync(body, patch, **_):
    _, custom_api, _ = _clients()
    delay = merged_step(body, patch, custom_api)
    if delay > 0:
        raise kopf.TemporaryError("waiting for Argo CD sync", delay=delay)


@kopf.on.field(GROUP, VERSION, PLURAL, field="status.phase", value=Phase.VERIFYING.value)
def verify_tick(body, patch, **_):
    """Polls by retrying (not ``@kopf.timer``, which would add a finalizer to the incident)."""
    core_api, custom_api, _ = _clients()
    verify_step(body, patch, core_api, custom_api)
    incidents.keep_polling(patch, Phase.VERIFYING, 10.0)


def _on_closed(body, patch, **_):
    _, _, repo = _clients()
    closed_step(body, patch, repo)


for _phase in TERMINAL_PHASES:
    kopf.on.field(GROUP, VERSION, PLURAL, field="status.phase", value=_phase.value,
                  id=f"closed-{_phase.value.lower()}")(_on_closed)
