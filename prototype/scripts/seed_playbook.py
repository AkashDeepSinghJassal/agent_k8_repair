"""DEV/TEST TOOL: seed a playbook from a recorded T3 shadow run, WITHOUT any merge.

Promotion T3 -> T2 -> T1 normally needs merged, verified heals (a human merges the PR). To exercise the T1 and T2
tiers live in shadow mode this script calls the real ``crystallize.capture`` on the saved agent trace
(``<state>/agent-traces/<incident>-a<attempt>.json``) and the proposal in the incident flow, as if the merged change
equalled the proposal, then forces the playbook state. The history entry says "seeded for testing ... (no merge)".
Counters are left as capture made them (no faked t3_ok / streak). Never use this for real results.

    uv run python scripts/seed_playbook.py --incident demo/ri-ef9d6f8d30-2 --state T1
    REPAIR_STATE_DIR=/scratch/dir uv run python scripts/seed_playbook.py ...   # work on a copy

The signature is the one the router saw for that incident (flow ``route`` event), cross-checked by recomputing it
from the stored evidence bundle with ``fingerprint.knowledge_signature``. Learn events go to the flow of a
synthetic incident ``seed-<incident>`` so recorded flows stay untouched.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

from repair_operator import crystallize, evidence, flow
from repair_operator.config import get_settings
from repair_operator.fingerprint import knowledge_signature
from repair_operator.playbooks import default_store, push_history


def _recompute(state: Path, incident: str) -> str | None:
    """Signature recomputed from the evidence bundle the way evidence.collect does (pod incidents)."""
    for f in sorted(glob.glob(str(state / "evidence" / f"{incident}-*.json"))):
        try:
            b = json.loads(Path(f).read_text())
            texts = [b["logs"]["current"], b["logs"]["previous"], [e["message"] for e in b.get("events", [])],
                     [b["failure"].get("message") or ""]]
            return knowledge_signature(b["failure"]["reason"], b["failure"]["exitCode"], "pod", evidence.error_lines(*texts))
        except (OSError, ValueError, KeyError):
            continue
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--incident", required=True, help="<namespace>/<name>")
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--state", required=True, choices=["T3", "T2", "T1"])
    a = ap.parse_args(argv)
    ns, _, name = a.incident.partition("/")
    state_dir = Path(get_settings().state_dir).expanduser()

    def fail(msg: str) -> int:
        print(f"seed_playbook: REFUSED: {msg}", file=sys.stderr)
        return 1

    events = flow.read(name, ns)
    if not events:
        return fail(f"no flow for {a.incident} in {state_dir}/flows")
    tpath = state_dir / "agent-traces" / f"{name}-a{a.attempt}.json"
    try:
        stored = json.loads(tpath.read_text())
    except (OSError, ValueError) as exc:
        return fail(f"cannot read agent trace {tpath}: {exc}")
    prop = next((e["data"] for e in reversed(events) if e["kind"] == "proposal"), None)
    route = next((e["data"] for e in reversed(events) if e["kind"] == "route"), None)
    if not prop or not prop.get("edits"):
        return fail("flow has no proposal with edits")
    sig = (route or {}).get("signature") or ""
    if not sig:
        return fail("flow route event has no signature")
    recomputed = _recompute(state_dir, name)
    pb = crystallize.capture(stored.get("trace") or [], prop, stored.get("seed") or {}, signature=sig)
    if pb is None:
        return fail("crystallize.capture returned None (no replayable steps, edits or file in the trace/proposal)")
    if a.state == "T1" and pb.t1_blocked:
        return fail(f"t1_blocked: {pb.t1_blocked}")
    reason = f"seeded for testing from {name} (no merge)"
    sid = f"seed-{name}"
    existing = default_store().get(pb.id)
    if existing is not None:
        print(f"note: replacing existing playbook {pb.id} (state {existing.state})", file=sys.stderr)
    push_history(pb, "captured", "-", "T3", f"DEV/TEST seed: {reason}", sid)
    crystallize.learn_event(sid, ns, "captured", pb, reason, tier="T3")
    if a.state != "T3":
        frm = "T3"
        for to in ("T2", "T1") if a.state == "T1" else ("T2",):
            pb.state = to
            push_history(pb, "promoted", frm, to, reason, sid)
            crystallize.learn_event(sid, ns, "promoted", pb, reason, frm=frm, to=to)
            frm = to
    pb.meta["seeded"] = reason
    default_store().upsert(pb)
    out = pb.to_dict()
    out["_check"] = {"signature_from_route": sig, "signature_recomputed_from_evidence": recomputed,
                     "match": recomputed == sig if recomputed else None, "t1_blocked": pb.t1_blocked,
                     "state_dir": str(state_dir)}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
