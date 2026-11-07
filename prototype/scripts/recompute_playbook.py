"""ONE-OFF, idempotent: re-evaluate a playbook's consistency with the essential-path rule, then try promotion.

Consistency used to compare raw tool sequences, so extra exploratory calls counted against an identical heal
(V4_CONTRACT.md section 8). This recomputes ``meta.seen`` / ``meta.same`` / ``consistency`` from the recorded T3 runs
(the incidents in the playbook history, or ``--incidents``; the first is the capturing run) with
``crystallize.is_consistent``, calls the normal ``maybe_promote`` and emits the ``learn`` event to the latest incident.
The db is backed up to ``playbooks.db.bak-recompute`` first (not overwritten if present).

    uv run python scripts/recompute_playbook.py --id pb-1bf9 [--incidents ri-a ri-b] [--namespace demo]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from repair_operator import crystallize
from repair_operator.config import get_settings
from repair_operator.playbooks import default_store, push_history

NOTE = "re-evaluated with essential-path consistency"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--id", required=True)
    ap.add_argument("--incidents", nargs="*", help="T3 run incident names, oldest first (default: from history)")
    ap.add_argument("--namespace", default="demo")
    a = ap.parse_args(argv)
    settings = get_settings()
    state = Path(settings.state_dir).expanduser()
    store = default_store(settings)
    pb = store.get(a.id)
    if pb is None:
        print(f"recompute_playbook: no playbook {a.id}", file=sys.stderr)
        return 1
    incidents = a.incidents or list(dict.fromkeys(
        h["incident"] for h in pb.history if h.get("event") in ("captured", "observed") and h.get("from") in ("-", "T3")
        and not str(h.get("incident", "")).startswith("seed-")))
    if len(incidents) < 2:
        print("recompute_playbook: need at least two recorded T3 runs", file=sys.stderr)
        return 1
    traces = []
    for inc in incidents:
        try:
            traces.append(json.loads((state / "agent-traces" / f"{inc}-a1.json").read_text()))
        except (OSError, ValueError) as exc:
            print(f"recompute_playbook: cannot read trace for {inc}: {exc}", file=sys.stderr)
            return 1
    before = {"state": pb.state, "seen": pb.meta.get("seen"), "same": pb.meta.get("same"), "consistency": pb.consistency}
    seen = same = 1  # the capturing run defines the playbook
    verdicts = {incidents[0]: "captured"}
    for inc, st in zip(incidents[1:], traces[1:], strict=True):
        fresh = crystallize.capture(st.get("trace") or [], st.get("proposal") or {}, st.get("seed") or {}, signature=pb.signature)
        ok = crystallize.is_consistent(pb, fresh, st.get("trace") or [])
        seen += 1
        same += ok
        verdicts[inc] = "consistent" if ok else "NOT consistent"
    if any(NOTE in h.get("reason", "") for h in pb.history):
        print(json.dumps({"id": pb.id, "already_applied": True, **before}, indent=2))
        return 0
    shutil.copy2(state / "playbooks.db", state / "playbooks.db.bak-recompute") if not (
        state / "playbooks.db.bak-recompute").exists() else None
    pb.meta["seen"], pb.meta["same"] = seen, same
    pb.consistency = round(same / seen, 4)
    last = incidents[-1]
    push_history(pb, "observed", pb.state, pb.state, f"consistency {pb.consistency:.2f} ({NOTE})", last)
    store.upsert(pb)
    new = crystallize.maybe_promote(pb, store, last, a.namespace, settings, note=NOTE)
    if not new:
        crystallize.learn_event(last, a.namespace, "observed", pb, f"consistency {pb.consistency:.2f} over {seen} runs ({NOTE})", tier="T3")
    pb = store.get(a.id)
    print(json.dumps({"id": pb.id, "before": before, "verdicts": verdicts, "after": {
        "state": pb.state, "seen": seen, "same": same, "consistency": pb.consistency, "runs": pb.runs, "streak": pb.meta.get("streak")},
        "promoted_to": new}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
