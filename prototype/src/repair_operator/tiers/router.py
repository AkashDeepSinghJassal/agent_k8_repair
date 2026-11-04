"""Router: knowledge signature -> playbook -> cheapest earned tier, with T1 -> T2 -> T3 fallback in one attempt."""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from .. import crystallize, flow
from ..config import Settings, get_settings
from ..models import Edit, Proposal
from ..playbooks import Playbook, PlaybookStore, default_store
from . import TierError, TierResult, Tracer, run_sync, t1, t2

log = logging.getLogger(__name__)


@asynccontextmanager
async def default_mcp():
    from repair_agent.mcp_tools import McpToolbox
    from repair_agent.servers import server_specs

    with tempfile.TemporaryDirectory(prefix="repair-replay-") as tmp:
        async with McpToolbox(server_specs(f"{tmp}/out.json")) as box:
            yield box


def _choose(pb: Playbook | None, feedback: list[str], s: Settings) -> tuple[list[str], str]:
    """(tier chain, reason in plain words)."""
    if pb is None:
        return ["T3"], "No playbook for this failure signature yet: running the full agent (T3) so the run can be crystallized"
    streak = pb.meta.get("streak", 0)
    if pb.state == "T3":
        return ["T3"], (f"Playbook {pb.id} is still a candidate (T3): {streak} of {s.promote_t2_after} verified agent runs "
                        "needed for T2, running the full agent")
    if pb.state == "T2":
        return ["T2", "T3"], (f"Playbook {pb.id} is T2 after {pb.runs['t3_ok']} verified agent runs: replaying the tool calls "
                              "and making one narrow LLM call")
    if feedback:
        return ["T2", "T3"], (f"Playbook {pb.id} is T1 but the previous attempt was rejected (feedback present): "
                              "skipping T1 for this attempt, using T2")
    return ["T1", "T2", "T3"], (f"Playbook {pb.id} is T1 after {pb.runs['t2_match']} verified hybrid runs: "
                                "replaying without LLM")


def _demote_if_rejected(pb: Playbook | None, store: PlaybookStore, inc: str, ns: str, feedback: list[str]) -> Playbook | None:
    """Feedback means the previous attempt of this incident was rejected: if it ran on this playbook's tier, demote."""
    if pb is None or not feedback or pb.state == "T3" or not inc:
        return pb
    events = flow.read(inc, ns)
    last = next((i for i in range(len(events) - 1, -1, -1) if events[i]["kind"] == "route"), None)
    if last is None:
        return pb
    prev = (events[last].get("data") or {}).get("chosen_tier")
    if prev not in ("T1", "T2") or pb.state != prev:
        return pb
    if any(e["kind"] == "learn" and (e.get("data") or {}).get("action") == "demoted" for e in events[last:]):
        return pb
    crystallize.demote(pb, store, f"previous {prev} attempt was rejected: {feedback[-1][:200]}", inc, ns)
    return store.get(pb.id)


def _to_proposal(pb: Playbook, res: TierResult) -> Proposal:
    prop = Proposal(
        **{"class": pb.cls}, root_cause=res.root_cause, confidence=res.confidence, fixable_in_git=True, file=res.file,
        edits=[Edit(**e) for e in res.edits], rationale=res.rationale, tier=3, source="agent",
        tier_used=res.tier, playbook_id=pb.id,
    )
    prop.agent_meta = {**res.meta, "status": "proposed", "trace": res.trace}
    prop.agent_evidence = res.evidence
    prop.agent_files = list(res.files)
    return prop


def analyze_hybrid(
    bundle: dict, feedback: list[str] | None = None, *, settings: Settings | None = None, store: PlaybookStore | None = None,
    mcp_factory: Callable[[], Any] | None = None, llm: t2.LlmFn | None = None, t3: Callable[..., Proposal] | None = None,
    seed: dict | None = None,
) -> Proposal:
    from ..analyzer.agent import agent_analyze, build_seed

    settings = settings or get_settings()
    feedback = list(feedback or [])
    store = store or default_store(settings)
    t3 = t3 or agent_analyze
    seed = seed or build_seed(bundle)
    inc, ns = str(bundle.get("incident") or ""), seed.get("namespace") or ""
    sig = bundle.get("signature") or ""
    pb = store.get_by_signature(sig) if sig else None
    pb = _demote_if_rejected(pb, store, inc, ns, feedback)
    chain, reason = _choose(pb, feedback, settings)

    def route(tier: str, why: str, fallback_from: str | None = None) -> None:
        flow.emit(inc, ns, "route", title=f"Route to {tier}: {why}", tier=tier, data={
            "signature": sig, "playbook_id": pb.id if pb else None, "playbook_state": pb.state if pb else None,
            "chosen_tier": tier, "tier": tier, "reason": why, "fallback_from": fallback_from,
            "counts": dict(pb.runs) if pb else {}}) if inc else None

    tried: list[tuple[str, str]] = []
    if pb is not None and chain[0] != "T3":
        async def attempt() -> Proposal | None:
            factory = mcp_factory or default_mcp
            try:
                cm = factory()
                box = await cm.__aenter__()
            except Exception as exc:  # noqa: BLE001
                tried.append((chain[0], f"could not open the MCP servers: {exc}"))
                return None
            try:
                for idx, tier in enumerate(chain[:-1]):
                    if idx == 0:
                        route(tier, reason)
                    else:
                        route(tier, f"{tried[-1][0]} failed ({tried[-1][1]}): falling back to {tier}", tried[-1][0])
                    tr = Tracer(inc, ns, tier)
                    try:
                        if tier == "T1":
                            res = await t1.run(pb, seed, box, tr)
                        else:
                            res = await t2.run(pb, seed, box, llm, tr, feedback or None)
                        return _to_proposal(pb, res)
                    except TierError as exc:
                        err = str(exc)
                    except Exception as exc:
                        log.exception("%s: %s crashed", inc, tier)
                        err = f"{type(exc).__name__}: {exc}"
                    tried.append((tier, err))
                    fresh = store.get(pb.id)
                    if fresh is not None and fresh.state == tier:
                        crystallize.demote(fresh, store, f"{tier} failed: {err[:200]}", inc, ns)
                return None
            finally:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    log.debug("closing MCP toolbox failed", exc_info=True)

        proposal = run_sync(attempt())
        if proposal is not None:
            return proposal
        route("T3", f"{tried[-1][0]} failed ({tried[-1][1]}): falling back to the full agent (T3)" if tried else reason,
              tried[-1][0] if tried else None)
    else:
        route("T3", reason)

    prop = t3(bundle, feedback or None)
    prop.tier_used = "T3"
    prop.playbook_id = pb.id if pb else None
    return prop
