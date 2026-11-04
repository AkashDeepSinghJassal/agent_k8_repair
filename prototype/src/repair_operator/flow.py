"""Per-incident flow trace (V4_CONTRACT.md section 1).

One JSON Lines file per incident under ``<state_dir>/flows/``. Writing never raises into the caller.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import get_settings

log = logging.getLogger(__name__)

MAX_TEXT = 20_000
_lock = threading.Lock()


def _dir() -> Path:
    return Path(get_settings().state_dir).expanduser() / "flows"


def path_for(incident: str, namespace: str) -> Path:
    return _dir() / f"{namespace}__{incident}.jsonl"


def _cap(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + f"…[truncated {len(value) - MAX_TEXT} chars]"
    if isinstance(value, dict):
        return {k: _cap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_cap(v) for v in value]
    return value


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(UTC).microsecond // 1000:03d}Z"


def emit(
    incident: str, namespace: str, kind: str, *, title: str, data: dict | None = None,
    tier: str | None = None, parent: str | None = None, status: str = "ok",
    duration_ms: int | None = None, event_id: str | None = None,
) -> str:
    """Append one event and return its id."""
    eid = event_id or uuid.uuid4().hex[:12]
    event = {
        "id": eid, "ts": _now(), "incident": incident, "namespace": namespace, "kind": kind,
        "title": title, "tier": tier, "parent": parent, "status": status, "data": _cap(data or {}),
    }
    if duration_ms is not None:
        event["duration_ms"] = duration_ms
    try:
        p = path_for(incident, namespace)
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, default=str)
        with _lock, p.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001  (tracing must never break the loop)
        log.warning("flow emit failed for %s/%s: %s", namespace, incident, exc)
    return eid


def emit_for(body: dict | None, kind: str, *, title: str, **kw: Any) -> str | None:
    """``emit`` for a RepairIncident CR body (name + namespace from metadata). Never raises; None when unknown."""
    try:
        meta = (body or {}).get("metadata") or {}
        name, ns = meta.get("name"), meta.get("namespace")
        if not name:
            return None
        return emit(name, ns or "", kind, title=title, **kw)
    except Exception as exc:  # noqa: BLE001  (tracing must never break the loop)
        log.warning("flow emit_for failed: %s", exc)
        return None


def read(incident: str, namespace: str) -> list[dict]:
    """All events, in write order. Later events with the same id (span ends) replace the earlier one."""
    p = path_for(incident, namespace)
    if not p.exists():
        return []
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev["id"] not in by_id:
            order.append(ev["id"])
            by_id[ev["id"]] = ev
        else:  # span end: keep the start time, take the final status, duration and merged data
            start = by_id[ev["id"]]
            by_id[ev["id"]] = {**start, **ev, "ts": start["ts"], "data": {**start.get("data", {}), **ev.get("data", {})}}
    return [by_id[i] for i in order]


def list_flows(limit: int = 20) -> list[tuple[str, str, float]]:
    """(namespace, incident, mtime) of the most recently written flows."""
    d = _dir()
    if not d.exists():
        return []
    files = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    out = []
    for f in files:
        ns, _, inc = f.stem.partition("__")
        out.append((ns, inc, f.stat().st_mtime))
    return out


@contextmanager
def span(incident: str, namespace: str, kind: str, *, title: str, data: dict | None = None,
         tier: str | None = None, parent: str | None = None):
    """Emit ``kind`` as running; on exit emit the same id as ok/error with duration and added data.

    Yields ``(event_id, extra)``; put result fields into ``extra``.
    """
    eid = emit(incident, namespace, kind, title=title, data=data, tier=tier, parent=parent, status="running")
    extra: dict = {}
    start = time.monotonic()
    status = "ok"
    try:
        yield eid, extra
    except Exception as exc:
        status = "error"
        extra.setdefault("error", str(exc)[:500])
        raise
    finally:
        emit(incident, namespace, kind, title=extra.pop("title", title), data=extra, tier=tier, parent=parent,
             status=extra.pop("status", status), duration_ms=int((time.monotonic() - start) * 1000), event_id=eid)
