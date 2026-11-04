"""Kopf entry point: ``kopf run -m repair_operator.main --all-namespaces``.

Handlers are thin; the logic lives in plain functions so it is unit-testable.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime

import kopf

from . import (
    detectors,  # noqa: F401  (registers the detector handlers)
    evidence,
    incidents,
    verify_handlers,  # noqa: F401
)
from .config import get_settings
from .incidents import GROUP, PLURAL, VERSION
from .models import Phase
from .repair_flow import analyze_step, propose_step, review_step

log = logging.getLogger(__name__)

# How often an open PR is polled for merge / review (issue #10). Override with REPAIR_PR_POLL_SECONDS.
PR_POLL_SECONDS = max(5, int(os.environ.get("REPAIR_PR_POLL_SECONDS", "30")))


def debounce_remaining(detected_iso: str | None, debounce: int, now: datetime | None = None) -> float:
    """Seconds still to wait before collecting evidence (0 when the debounce has elapsed)."""
    if not detected_iso:
        return float(debounce)
    detected = datetime.strptime(detected_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    elapsed = ((now or datetime.now(UTC)) - detected).total_seconds()
    return max(0.0, debounce - elapsed)


def collect_step(
    body: dict, patch: dict, debounce: int, now: datetime | None = None, clients: tuple | None = None
) -> float:
    """Detected -> Collected | Dismissed after the debounce. Returns the delay if still waiting, else 0.

    ``clients`` is ``(core_api, custom_api, github)``; default: built from the environment.
    """
    status = body.get("status") or {}
    remaining = debounce_remaining((status.get("timestamps") or {}).get("detected"), debounce, now)
    if remaining > 0:
        return remaining
    if not (body.get("spec") or {}).get("source"):  # nothing to collect from
        incidents.set_phase(patch, Phase.COLLECTED, now, incident=body)
        return 0.0
    evidence.handle_collect(body, patch, clients, now)
    return 0.0


def analyze_hook(body: dict, patch: dict) -> None:
    """Collected -> Analyzing, then the analyzer runs."""
    incidents.set_phase(
        patch, Phase.ANALYZING, incident=body, attempts=(body.get("status") or {}).get("attempts") or 1
    )


def active_fault(namespace: str, max_age_s: float = 1800) -> str | None:
    """Fault id the dashboard injected into ``namespace`` in the last 30 min (active-faults.json). Never raises."""
    try:
        path = get_settings().state_dir / "active-faults.json"
        ent = json.loads(path.read_text()).get(namespace) or {}
        if ent.get("fault") and time.time() - float(ent.get("ts", 0)) < max_age_s:
            return str(ent["fault"])
    except (OSError, ValueError, TypeError, AttributeError):
        log.debug("active-faults read failed", exc_info=True)
    return None


def emit_detected(body: dict) -> None:
    """Flow trace: ``detected`` event from the new incident's spec. Never raises."""
    try:
        from . import flow

        spec = body.get("spec") or {}
        src = spec.get("source") or {}
        data = {"source": {k: src.get(k) for k in ("kind", "name", "owner", "container", "reason", "classHint")},
                "fingerprint": spec.get("fingerprint")}
        fault = active_fault((body.get("metadata") or {}).get("namespace") or src.get("namespace") or "")
        if fault:
            data["fault"] = fault
        flow.emit_for(
            body, "detected",
            title=f"Detected {src.get('reason') or 'failure'} on {src.get('kind', 'Pod')} {src.get('name', '')}".strip(),
            data=data,
        )
    except Exception:
        log.debug("detected flow event failed", exc_info=True)


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(prefix=GROUP)
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(prefix=GROUP)
    settings.watching.server_timeout = 60  # a silently dead watch stream is re-opened within ~a minute, not 10+
    settings.watching.client_timeout = 90
    log.info("repair operator starting with %s", get_settings().public())


@kopf.on.create(GROUP, VERSION, PLURAL)
def incident_created(body, patch, **_):
    incidents.set_phase(patch, Phase.DETECTED, incident=body)
    emit_detected(body)


@kopf.on.field(GROUP, VERSION, PLURAL, field="status.phase", value=Phase.DETECTED.value)
def on_detected(body, patch, **_):
    if not incidents.in_phase(body, Phase.DETECTED):
        return  # Kopf also fires a value-filtered field handler when the field leaves the value
    delay = collect_step(body, patch, get_settings().debounce_seconds)
    if delay > 0:
        raise kopf.TemporaryError("debouncing", delay=delay)


@kopf.on.field(GROUP, VERSION, PLURAL, field="status.phase", value=Phase.COLLECTED.value)
def on_collected(body, patch, **_):
    if not incidents.in_phase(body, Phase.COLLECTED):
        return
    analyze_hook(body, patch)


@kopf.on.field(GROUP, VERSION, PLURAL, field="status.phase", value=Phase.ANALYZING.value)
def on_analyzing(body, patch, **_):
    """Analyzing -> Proposed | Escalated (analyzer + guardrails, retries internally)."""
    if not incidents.in_phase(body, Phase.ANALYZING):
        return  # Analyzing -> Escalated fires this handler again: never re-run the agent
    analyze_step(body, patch)


@kopf.on.field(GROUP, VERSION, PLURAL, field="status.phase", value=Phase.PROPOSED.value)
def on_proposed(body, patch, **_):
    """Proposed -> AwaitingReview (branch + PR)."""
    if not incidents.in_phase(body, Phase.PROPOSED):
        return
    propose_step(body, patch)


@kopf.on.field(GROUP, VERSION, PLURAL, field="status.phase", value=Phase.AWAITING_REVIEW.value)
def on_review_poll(body, patch, **_):
    """AwaitingReview -> Merged | Analyzing (changes requested) | Failed (closed).

    Polls by retrying (not ``@kopf.timer``, which would add a finalizer to the incident).
    """
    if not incidents.in_phase(body, Phase.AWAITING_REVIEW):
        return  # stale retry of a finished poll
    review_step(body, patch)
    incidents.keep_polling(patch, Phase.AWAITING_REVIEW, PR_POLL_SECONDS)
