"""Shared pydantic models (CONTRACTS section 7) and the incident phase enum."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Phase(str, Enum):
    DETECTED = "Detected"
    COLLECTED = "Collected"
    DISMISSED = "Dismissed"
    ANALYZING = "Analyzing"
    PROPOSED = "Proposed"
    AWAITING_REVIEW = "AwaitingReview"
    MERGED = "Merged"
    VERIFYING = "Verifying"
    HEALED = "Healed"
    FAILED = "Failed"
    ESCALATED = "Escalated"


TERMINAL_PHASES = frozenset({Phase.DISMISSED, Phase.HEALED, Phase.FAILED, Phase.ESCALATED})


class Edit(BaseModel):
    find: str
    replace: str


class Proposal(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    cls: str = Field(alias="class")
    root_cause: str
    confidence: float
    fixable_in_git: bool
    file: str | None = None
    edits: list[Edit] = []
    template: str | None = None
    params: dict = {}
    rationale: str = ""
    verify: str = ""
    needs_remeasure: bool = False
    rule_disagreement: dict | None = None
    tier: int
    source: Literal["rules", "llm", "recipe"]
