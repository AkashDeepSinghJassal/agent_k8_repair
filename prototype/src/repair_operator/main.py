"""Kopf entry point: ``kopf run -m repair_operator.main --all-namespaces``.

Handlers are thin; the logic lives in plain functions so it is unit-testable.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import kopf

from . import (
    detectors,  # noqa: F401  (registers the detector handlers)
    evidence,
    incidents,
    retrigger,
)
from .config import get_settings
from .incidents import GROUP, PLURAL, VERSION
from .models import Phase

log = logging.getLogger(__name__)


def debounce_remaining(detected_iso: str | None, debounce: int, now: datetime | None = None) -> float:
    """Seconds still to wait before collecting evidence (0 when the debounce has elapsed)."""
    if not detected_iso:
        return float(debounce)
    detected = datetime.strptime(detected_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    elapsed = ((now or datetime.now(UTC)) - detected).total_seconds()
    return max(0.0, debounce - elapsed)


def collect_step(body: dict, patch: dict, debounce: int, now: datetime | None = None) -> float:
    """Detected -> Collected after the debounce. Returns the delay if still waiting, else 0."""
    status = body.get("status") or {}
    remaining = debounce_remaining((status.get("timestamps") or {}).get("detected"), debounce, now)
    if remaining > 0:
        return remaining
    # TODO(#6): evidence.collect also decides Dismissed (pod recovered within the debounce window)
    result = evidence.collect(body) or {}
    incidents.set_phase(patch, Phase.COLLECTED, **result)
    return 0.0


def analyze_hook(body: dict, patch: dict) -> None:
    """Collected -> Analyzing, then the analyzer runs."""
    incidents.set_phase(patch, Phase.ANALYZING, attempts=(body.get("status") or {}).get("attempts", 0) + 1)
    # TODO(#7/#10): analyze(bundle, feedback) -> Proposal; guardrails; open or update the PR.


def retrigger_hook(body: dict, patch: dict) -> None:
    """Merged -> Verifying."""
    result = retrigger.retrigger(body) or {}
    incidents.set_phase(patch, Phase.VERIFYING, **result)
    # TODO(#11): wait for Argo CD sync, resubmit the failed workflow, run the verify window.


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(prefix=GROUP)
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(prefix=GROUP)
    log.info("repair operator starting with %s", get_settings().public())


@kopf.on.create(GROUP, VERSION, PLURAL)
def incident_created(body, patch, **_):
    incidents.set_phase(patch, Phase.DETECTED)


@kopf.on.field(GROUP, VERSION, PLURAL, field="status.phase", value=Phase.DETECTED.value)
def on_detected(body, patch, **_):
    delay = collect_step(body, patch, get_settings().debounce_seconds)
    if delay > 0:
        raise kopf.TemporaryError("debouncing", delay=delay)


@kopf.on.field(GROUP, VERSION, PLURAL, field="status.phase", value=Phase.COLLECTED.value)
def on_collected(body, patch, **_):
    analyze_hook(body, patch)


@kopf.on.field(GROUP, VERSION, PLURAL, field="status.phase", value=Phase.MERGED.value)
def on_merged(body, patch, **_):
    retrigger_hook(body, patch)
