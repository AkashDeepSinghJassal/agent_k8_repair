"""Phase event log: one JSON line per phase change (CONTRACTS sections 3 and 4).

Each event is printed to stdout and appended to ``REPAIR_STATE_DIR/events.jsonl``.
``emit`` never raises: metrics must not break the operator.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from .config import get_settings

log = logging.getLogger(__name__)

EVENTS_FILE = "events.jsonl"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def events_path():
    """Path of the append-only event log under the state dir."""
    return get_settings().state_dir / EVENTS_FILE


def emit(incident_name: str, namespace: str | None, phase: Any, **fields: Any) -> dict:
    """Record a phase change. Returns the event dict (also when writing failed).

    Standard keys: ``ts``, ``incident``, ``namespace``, ``phase``. Extra keys such as
    ``class``, ``tier``, ``source``, ``attempt`` and ``fault`` are included when given;
    ``None`` values are dropped.
    """
    event: dict[str, Any] = {
        "ts": _now_iso(),
        "incident": incident_name,
        "namespace": namespace,
        "phase": getattr(phase, "value", phase),
    }
    event.update({k: v for k, v in fields.items() if v is not None})
    try:
        line = json.dumps(event, default=str, sort_keys=True)
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - default=str makes this unreachable
        log.warning("could not serialise metrics event: %s", exc)
        return event

    try:
        print(line, file=sys.stdout, flush=True)
    except Exception as exc:  # noqa: BLE001 - metrics must never break the operator
        log.warning("could not print metrics event: %s", exc)

    try:
        path = events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 - metrics must never break the operator
        log.warning("could not append metrics event to events.jsonl: %s", exc)
    return event
