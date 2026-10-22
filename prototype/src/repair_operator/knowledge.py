"""SQLite recipe store (CONTRACTS section 9).

Status ladder: candidate -> verified -> promoted, with promoted -> demoted on any failure or rejection.
Only ``promoted`` recipes are returned by ``lookup``. A connection is opened per call, so the store is
safe to use from several threads.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings, get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    signature TEXT PRIMARY KEY,
    class TEXT NOT NULL,
    template TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'candidate',
    approved INTEGER NOT NULL DEFAULT 0,
    rerun_passed INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
)
"""

# Outcome vocabulary for record_outcome.
HEALED_OUTCOMES = frozenset({"healed", "rerun_passed", "verified"})
APPROVED_OUTCOMES = frozenset({"approved", "merged"})
BAD_OUTCOMES = frozenset({"failed", "rejected", "escalated", "closed"})


@dataclass
class Recipe:
    signature: str
    cls: str
    template: str
    params: dict = field(default_factory=dict)
    status: str = "candidate"
    approved: int = 0
    rerun_passed: int = 0
    rejected: int = 0
    updated_at: float = 0.0


def _row(row: sqlite3.Row) -> Recipe:
    return Recipe(
        signature=row["signature"],
        cls=row["class"],
        template=row["template"],
        params=json.loads(row["params_json"] or "{}"),
        status=row["status"],
        approved=row["approved"],
        rerun_passed=row["rerun_passed"],
        rejected=row["rejected"],
        updated_at=row["updated_at"],
    )


class RecipeStore:
    def __init__(self, path: Path | str, promotion_threshold: int = 3):
        self.path = Path(path).expanduser()
        self.threshold = promotion_threshold

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute(_SCHEMA)
        return conn

    def get(self, signature: str) -> Recipe | None:
        """Any status (for inspection and tests)."""
        with closing(self._connect()) as c:
            row = c.execute("SELECT * FROM recipes WHERE signature=?", (signature,)).fetchone()
        return _row(row) if row else None

    def lookup(self, signature: str) -> Recipe | None:
        r = self.get(signature)
        return r if r and r.status == "promoted" else None

    def record_outcome(
        self, signature: str, cls: str, template: str | None, params: dict | None, outcome: str
    ) -> Recipe | None:
        """Record one outcome. Returns the updated recipe, or None if there is no template to learn from."""
        if not template:
            return None
        with closing(self._connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")  # serialise the read-modify-write across threads/processes
            row = c.execute("SELECT * FROM recipes WHERE signature=?", (signature,)).fetchone()
            r = _row(row) if row else Recipe(signature, cls, template, params or {})
            if outcome in HEALED_OUTCOMES:
                r.rerun_passed += 1
                if r.status == "candidate":
                    r.status = "verified"
                if r.status == "verified" and r.rerun_passed >= self.threshold:
                    r.status = "promoted"
                if r.status != "demoted":
                    r.cls, r.template, r.params = cls, template, params or r.params
            elif outcome in APPROVED_OUTCOMES:
                r.approved += 1
            elif outcome in BAD_OUTCOMES:
                r.rejected += 1
                if r.status == "promoted":
                    r.status = "demoted"
            else:
                raise ValueError(f"unknown outcome: {outcome}")
            r.updated_at = time.time()
            c.execute(
                "INSERT OR REPLACE INTO recipes(signature,class,template,params_json,status,approved,"
                "rerun_passed,rejected,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (r.signature, r.cls, r.template, json.dumps(r.params), r.status, r.approved,
                 r.rerun_passed, r.rejected, r.updated_at),
            )
        return r

    def similar(self, signature: str) -> list[Recipe]:
        """Stub for now (plan v2 retrieval)."""
        return []


def default_store(settings: Settings | None = None) -> RecipeStore:
    s = settings or get_settings()
    return RecipeStore(Path(s.state_dir).expanduser() / "knowledge.db", s.promotion_threshold)


def lookup(signature: str) -> Recipe | None:
    return default_store().lookup(signature)


def record_outcome(signature: str, cls: str, template: str | None, params: dict | None, outcome: str):
    return default_store().record_outcome(signature, cls, template, params, outcome)


def similar(signature: str) -> list[Recipe]:
    return default_store().similar(signature)
