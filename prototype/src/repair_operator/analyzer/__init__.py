"""Analyzer: ``analyze(bundle, feedback) -> Proposal`` (CONTRACTS section 7).

Modes: ``llm-first`` (default) tier 0 recipe, then always the LLM (rule table is only a hint, disagreement is
recorded); ``rules-first``: tier 0, tier 1 rules + template, then the LLM. Tier 4 escalates.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..knowledge import RecipeStore, default_store
from ..models import Proposal
from . import rules, templates
from .llm import InvalidOutput, LlmProposal, LlmUnavailable, call_llm, make_client

__all__ = ["InvalidOutput", "LlmProposal", "LlmUnavailable", "analyze"]

log = logging.getLogger(__name__)
LOW_CONFIDENCE = 0.5
DISAGREE_PENALTY = 0.15
OPEN_ENDED = frozenset({"CRASH_LOOP", "STAGE_FAILURE"})
TEMPLATE_CLASSES = frozenset(rules.TEMPLATE_FOR_CLASS)


def _path(bundle: dict) -> str | None:
    return (bundle.get("git") or {}).get("path")


def _escalate(cls: str, root_cause: str, source: str, *, confidence: float = 1.0, rationale: str = "", bundle: dict | None = None) -> Proposal:
    return Proposal(
        **{"class": cls},
        root_cause=root_cause,
        confidence=confidence,
        fixable_in_git=False,
        file=None,
        rationale=rationale,
        tier=4,
        source=source,
    )


def analyze(
    bundle: dict,
    feedback: list[str] | None = None,
    *,
    store: RecipeStore | None = None,
    client: Any = None,
    settings: Settings | None = None,
) -> Proposal:
    """Raises ``InvalidOutput`` (``.reason == "invalid_output"``) when the LLM refuses or fails validation twice."""
    feedback = list(feedback or [])
    settings = settings or get_settings()
    file = _path(bundle)

    # Tier 0: promoted recipe. Skipped when a reviewer rejected a previous attempt.
    signature = bundle.get("signature")
    if signature and not feedback:
        store = store or default_store(settings)
        recipe = store.lookup(signature)
        if recipe:
            edits = templates.render(recipe.template, bundle, recipe.params)
            if edits:
                return Proposal(
                    **{"class": recipe.cls},
                    root_cause=f"Promoted recipe {recipe.template} for signature {signature}",
                    confidence=1.0,
                    fixable_in_git=True,
                    file=file,
                    edits=edits,
                    template=recipe.template,
                    params=recipe.params,
                    rationale="Matched a promoted recipe; rendered deterministically without the LLM.",
                    tier=0,
                    source="recipe",
                )

    # Tier 1 (and early tier 4): rules.
    rule = rules.classify(bundle)
    if rule and rule.cls == "UNSCHEDULABLE":
        return _escalate("UNSCHEDULABLE", rule.hint, "rules", rationale="Scheduling failures are cluster capacity issues, not git changes.")
    rules_first = settings.analyzer_mode == "rules-first"
    if rules_first and rule and rule.template and not feedback:
        params = {}
        container = (bundle.get("source") or {}).get("container")
        if container:
            params["container"] = container
        edits = templates.render(rule.template, bundle, params)
        if edits:
            return Proposal(
                **{"class": rule.cls},
                root_cause=rule.hint,
                confidence=rule.confidence,
                fixable_in_git=True,
                file=file,
                edits=edits,
                template=rule.template,
                params=params,
                rationale=f"Rule matched {rule.cls}; template {rule.template} rendered on the current file.",
                tier=1,
                source="rules",
            )

    # Tier 2/3: LLM.
    replay_dir = Path(settings.state_dir).expanduser() / "replay"
    out = call_llm(
        bundle, feedback, f"{rule.cls} (confidence {rule.confidence}); patch hint: {rule.hint}. This is only a hint; decide from the evidence." if rule else None,
        client=client or make_client(settings), settings=settings, replay_dir=replay_dir,
    )
    prop = _from_llm(out, bundle, file)
    if rule and rule.cls != prop.cls:
        prop.rule_disagreement = {"rule": rule.cls, "llm": prop.cls}
        prop.confidence = round(max(0.0, prop.confidence - DISAGREE_PENALTY), 4)
        log.warning("%s: rule table says %s but LLM says %s", bundle.get("incident"), rule.cls, prop.cls)
    return prop


def _from_llm(out: LlmProposal, bundle: dict, file: str | None) -> Proposal:
    cls = out.cls
    base = {"root_cause": out.root_cause, "confidence": out.confidence, "rationale": out.rationale, "verify": out.verify}
    if cls == "UNSCHEDULABLE" or not out.fixable_in_git or out.confidence < LOW_CONFIDENCE:
        return Proposal(**{"class": cls}, fixable_in_git=False, file=None, tier=4, source="llm", **base)

    params = out.params_dict()
    if out.template and cls in TEMPLATE_CLASSES:
        edits = templates.render(out.template, bundle, params)
        if edits:
            return Proposal(
                **{"class": cls}, fixable_in_git=True, file=file, edits=edits, template=out.template,
                params=params, needs_remeasure=out.needs_remeasure, tier=2, source="llm", **base,
            )
    elif cls in OPEN_ENDED and out.edits and not out.template:
        return Proposal(
            **{"class": cls}, fixable_in_git=True, file=out.file or file, edits=out.edits,
            needs_remeasure=out.needs_remeasure, tier=3, source="llm", **base,
        )
    # Template could not render safely, or the shape does not fit the class: a human decides.
    base["rationale"] = (out.rationale + " | no safe deterministic or find/replace patch available").strip(" |")
    base.pop("confidence")
    return Proposal(**{"class": cls}, confidence=out.confidence, fixable_in_git=False, file=None, tier=4, source="llm", **base)
