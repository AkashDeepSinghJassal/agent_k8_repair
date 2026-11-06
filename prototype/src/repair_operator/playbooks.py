"""Playbooks: crystallized agent runs, stored in SQLite (V4_CONTRACT.md section 2)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings, get_settings

STATES = ("T3", "T2", "T1")
_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Step:
    """One replayable tool call; ``args`` string values may contain ``${var}`` placeholders."""

    server: str
    tool: str
    args: dict = field(default_factory=dict)


@dataclass
class Extract:
    """How to get one variable from the output of ``steps[from_step]`` (0-based index)."""

    var: str
    from_step: int
    rule: dict = field(default_factory=dict)


def _runs() -> dict:
    return {"t3_ok": 0, "t2_ok": 0, "t1_ok": 0, "fail": 0, "edited": 0, "rejected": 0, "t2_match": 0, "mismatch_streak": 0}


@dataclass
class Playbook:
    id: str
    signature: str
    cls: str
    state: str = "T3"
    steps: list[Step] = field(default_factory=list)
    extracts: list[Extract] = field(default_factory=list)
    file_template: str = ""
    edits_template: list[dict] = field(default_factory=list)
    action_sequence: list[str] = field(default_factory=list)
    runs: dict = field(default_factory=_runs)
    consistency: float = 1.0
    history: list[dict] = field(default_factory=list)
    created: str = field(default_factory=now_iso)
    updated: str = field(default_factory=now_iso)
    meta: dict = field(default_factory=dict)  # t1_blocked, seen, same, streak, root_cause, confidence

    @property
    def t1_blocked(self) -> str | None:
        return self.meta.get("t1_blocked")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Playbook:
        d = dict(d)
        d["steps"] = [Step(**s) for s in d.get("steps") or []]
        d["extracts"] = [Extract(**e) for e in d.get("extracts") or []]
        d["runs"] = {**_runs(), **(d.get("runs") or {})}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def playbook_id(signature: str, length: int = 4) -> str:
    return "pb-" + hashlib.sha1(signature.encode()).hexdigest()[:length]


class PlaybookStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS playbooks (id TEXT PRIMARY KEY, signature TEXT UNIQUE NOT NULL, data TEXT NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def _load(self, row: tuple | None) -> Playbook | None:
        return Playbook.from_dict(json.loads(row[0])) if row else None

    def get(self, pb_id: str) -> Playbook | None:
        with _lock, self._conn() as c:
            return self._load(c.execute("SELECT data FROM playbooks WHERE id=?", (pb_id,)).fetchone())

    def get_by_signature(self, sig: str) -> Playbook | None:
        with _lock, self._conn() as c:
            return self._load(c.execute("SELECT data FROM playbooks WHERE signature=?", (sig,)).fetchone())

    def upsert(self, pb: Playbook) -> Playbook:
        pb.updated = now_iso()
        with _lock, self._conn() as c:
            c.execute(
                "INSERT INTO playbooks (id, signature, data) VALUES (?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET signature=excluded.signature, data=excluded.data",
                (pb.id, pb.signature, json.dumps(pb.to_dict())),
            )
        return pb

    def list_all(self) -> list[Playbook]:
        with _lock, self._conn() as c:
            rows = c.execute("SELECT data FROM playbooks ORDER BY id").fetchall()
        return [Playbook.from_dict(json.loads(r[0])) for r in rows]

    def record(self, pb_id: str, event: str, incident: str, ok: bool, reason: str = "") -> Playbook | None:
        """Count a run (``event`` is ``t3``, ``t2`` or ``t1``; ``ok`` bumps ``<event>_ok`` else ``fail``) and log it."""
        pb = self.get(pb_id)
        if pb is None:
            return None
        key = f"{event}_ok" if ok else "fail"
        pb.runs[key] = pb.runs.get(key, 0) + 1
        pb.history.append({"ts": now_iso(), "event": event if ok else f"{event}_fail", "from": pb.state, "to": pb.state,
                           "reason": reason, "incident": incident})
        return self.upsert(pb)


def default_store(settings: Settings | None = None) -> PlaybookStore:
    s = settings or get_settings()
    return PlaybookStore(Path(s.state_dir).expanduser() / "playbooks.db")


def push_history(pb: Playbook, event: str, frm: str, to: str, reason: str, incident: str) -> None:
    pb.history.append({"ts": now_iso(), "event": event, "from": frm, "to": to, "reason": reason, "incident": incident})
    del pb.history[:-200]


def summary(pb: Playbook) -> dict[str, Any]:
    return {"id": pb.id, "state": pb.state, "runs": pb.runs, "consistency": pb.consistency}


def list_all() -> list[Playbook]:
    """All playbooks of the default store (used by the dashboard)."""
    return default_store().list_all()
