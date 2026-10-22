"""RepairIncident creation and phase bookkeeping."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from . import metrics
from .config import get_settings
from .models import TERMINAL_PHASES, Phase

log = logging.getLogger(__name__)

GROUP = "repair.example.dev"
VERSION = "v1alpha1"
PLURAL = "repairincidents"

# phase -> status.timestamps keys written when entering it
PHASE_TIMESTAMP: dict[Phase, tuple[str, ...]] = {
    Phase.DETECTED: ("detected",),
    Phase.COLLECTED: ("collected",),
    Phase.ANALYZING: (),
    Phase.PROPOSED: ("analyzed", "proposed"),
    Phase.AWAITING_REVIEW: ("prOpened",),
    Phase.MERGED: ("merged",),
    Phase.VERIFYING: ("synced",),
    Phase.HEALED: ("verified", "closed"),
    Phase.FAILED: ("closed",),
    Phase.ESCALATED: ("closed",),
    Phase.DISMISSED: ("closed",),
}

# fingerprint -> monotonic time the cooldown ends
_cooldowns: dict[str, float] = {}


def utc_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def incident_name(fp: str) -> str:
    return f"ri-{fp[:10]}"


def start_cooldown(fp: str, seconds: int | None = None, now: float | None = None) -> None:
    secs = get_settings().cooldown_seconds if seconds is None else seconds
    _cooldowns[fp] = (time.monotonic() if now is None else now) + secs


def in_cooldown(fp: str, now: float | None = None) -> bool:
    end = _cooldowns.get(fp)
    if end is None:
        return False
    if (time.monotonic() if now is None else now) >= end:
        del _cooldowns[fp]
        return False
    return True


def clear_cooldowns() -> None:
    _cooldowns.clear()


def _find_open_sibling(api: Any, namespace: str, source: dict, name: str) -> dict | None:
    """Non-terminal incident for the same owner workload + container (a different signal for one failure)."""
    owner, container = source.get("owner"), source.get("container")
    if source.get("kind") != "Pod" or not owner or not container:
        return None
    try:
        items = api.list_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL).get("items") or []
    except Exception:  # noqa: BLE001
        return None
    terminal = {p.value for p in TERMINAL_PHASES}
    for it in items if isinstance(items, list) else []:
        src = (it.get("spec") or {}).get("source") or {}
        phase = (it.get("status") or {}).get("phase")
        if (
            it.get("metadata", {}).get("name") != name
            and src.get("kind") == "Pod"
            and src.get("owner") == owner
            and src.get("container") == container
            and phase not in terminal
        ):
            return it
    return None


def _merge_signal(api: Any, namespace: str, existing: dict, source: dict) -> dict:
    """Record ``source`` as an extra signal on ``existing``; OOMKilled wins as the class hint."""
    name = existing["metadata"]["name"]
    signal = {k: source.get(k) for k in ("name", "reason", "classHint") if source.get(k)}
    signals = [*((existing.get("status") or {}).get("signals") or [])]
    if signal and signal not in signals:
        signals.append(signal)
        api.patch_namespaced_custom_object_status(GROUP, VERSION, namespace, PLURAL, name, {"status": {"signals": signals}})
    old = (existing.get("spec") or {}).get("source") or {}
    if source.get("classHint") == "OOM_KILLED" and old.get("classHint") != "OOM_KILLED":
        api.patch_namespaced_custom_object(
            GROUP, VERSION, namespace, PLURAL, name,
            {"spec": {"source": {"classHint": "OOM_KILLED", "reason": source.get("reason")}}},
        )
    log.info("signal %s merged into open incident %s", source.get("reason"), name)
    return existing


def open_incident(
    api: Any, namespace: str, source: dict, fp: str, signature: str | None = None
) -> dict | None:
    """Create ``ri-<fp[:10]>``. Returns the CR, or the existing one on HTTP 409.

    Returns None when the fingerprint is in cooldown. ``api`` is a
    ``kubernetes.client.CustomObjectsApi`` (or a mock of it).
    """
    if in_cooldown(fp):
        log.info("fingerprint %s in cooldown, not opening an incident", fp[:10])
        return None
    name = incident_name(fp)
    sibling = _find_open_sibling(api, namespace, source, name)
    if sibling is not None:
        return _merge_signal(api, namespace, sibling, source)
    body: dict[str, Any] = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "RepairIncident",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"source": {**source, "namespace": namespace}, "fingerprint": fp},
    }
    if signature:
        body["metadata"]["annotations"] = {f"{GROUP}/signature": signature}
    try:
        return api.create_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, body)
    except Exception as exc:  # kubernetes.client.ApiException
        if getattr(exc, "status", None) == 409:
            log.info("incident %s already exists", name)
            return api.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
        raise


def set_phase(
    patch: Any,
    phase: Phase | str,
    now: datetime | None = None,
    incident: dict | str | None = None,
    namespace: str | None = None,
    **fields: Any,
) -> None:
    """Write status.phase, status.timestamps.<key> (ISO-8601 UTC) and extra status fields.

    ``incident`` is the RepairIncident body (dict) or its name; when known, the phase change
    is also emitted to the event log (``metrics.emit``). Callers that omit it emit nothing.
    """
    phase = Phase(phase)
    status = patch.setdefault("status", {})
    status["phase"] = phase.value
    keys = PHASE_TIMESTAMP[phase]
    if keys:
        stamp = utc_iso(now)
        status.setdefault("timestamps", {}).update({k: stamp for k in keys})
    status.update(fields)

    if incident is not None:
        if isinstance(incident, dict):
            meta = incident.get("metadata") or {}
            name, ns = meta.get("name"), namespace or meta.get("namespace")
        else:
            name, ns = incident, namespace
        if name:
            classification = status.get("classification") or {}
            metrics.emit(
                name,
                ns,
                phase.value,
                **{
                    "class": classification.get("class"),
                    "tier": (status.get("proposal") or {}).get("tier"),
                    "source": classification.get("source"),
                    "attempt": status.get("attempts"),
                    "fault": fields.get("fault") or status.get("fault"),
                },
            )


def keep_polling(patch: dict, phase: Phase, delay: float) -> None:
    """Re-run the calling ``on.field(status.phase)`` handler after ``delay`` seconds while still in ``phase``.

    Used instead of ``@kopf.timer``: timers make Kopf add a finalizer to every matching incident, which
    blocks ``kubectl delete`` whenever the operator is down. Retried field handlers need none.
    """
    import kopf

    if (patch.get("status") or {}).get("phase", phase.value) == phase.value:
        raise kopf.TemporaryError(f"polling {phase.value}", delay=delay)
