"""Read-only scenario checker: one-screen summary of an incident's flow, with optional PASS/FAIL expectations.

    uv run python scripts/scenario_check.py demo/ri-ef9d6f8d30-2 --expect-tier T1 --expect-outcome Escalated --expect-no-llm

Reads ``<state_dir>/flows/<ns>__<name>.jsonl`` (REPAIR_STATE_DIR respected). Only the latest run in the flow
(from the last ``detected`` event) is considered. Exit 0 when every given expectation holds, 1 otherwise, 2 when
there is no flow. Never writes anything and never touches the cluster.
"""

from __future__ import annotations

import argparse
import sys

from repair_operator import flow

STAGES = [
    ("Detected", ("detected",)), ("Evidence", ("evidence",)), ("Route", ("route",)),
    ("Investigate/Replay", ("agent_start", "tool_call", "llm_call", "extract")), ("Proposal", ("proposal",)),
    ("Guardrails", ("guardrail",)), ("PR", ("pr",)), ("Review", ("review",)), ("Sync", ("sync",)),
    ("Verify", ("verify",)), ("Outcome", ("outcome",)), ("Learn", ("learn",)),
]


def latest_run(events: list[dict]) -> list[dict]:
    idx = [i for i, e in enumerate(events) if e["kind"] == "detected"]
    return events[idx[-1]:] if idx else events


def summarize(events: list[dict]) -> dict:
    kinds = {e["kind"] for e in events}
    routes = [e for e in events if e["kind"] == "route"]
    chain = []
    for r in routes:
        d = r.get("data") or {}
        t = d.get("chosen_tier") or r.get("tier")
        chain.append(f"{t}" + (f" (fallback from {d['fallback_from']})" if d.get("fallback_from") else ""))
    final = next((e for e in reversed(events) if e["kind"] == "proposal"), None)
    tier = (final or {}).get("tier") or ((routes[-1].get("data") or {}).get("chosen_tier") if routes else None)
    calls = [e for e in events if e["kind"] == "tool_call"]
    llm = [e for e in events if e["kind"] == "llm_call"]
    agent = [e for e in events if e["kind"] == "agent_start"]
    out = next((e for e in reversed(events) if e["kind"] == "outcome"), None)
    learn = next((e for e in reversed(events) if e["kind"] == "learn"), None)
    return {
        "present": [n for n, ks in STAGES if kinds & set(ks)], "missing": [n for n, ks in STAGES if not kinds & set(ks)],
        "tier": tier, "chain": chain, "route": routes[-1].get("data") if routes else {}, "calls": calls,
        "extracts": [e for e in events if e["kind"] == "extract"], "llm": llm, "agent_runs": agent,
        "outcome": (out or {}).get("data") or {}, "learn": (learn or {}).get("data") or {},
        "learn_title": (learn or {}).get("title", ""), "guardrails": [e for e in events if e["kind"] == "guardrail"],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("incident", help="<namespace>/<name>")
    ap.add_argument("--expect-tier", choices=["T1", "T2", "T3"])
    ap.add_argument("--expect-outcome", help="outcome phase, e.g. Healed, Escalated, Failed")
    ap.add_argument("--expect-no-llm", action="store_true", help="no llm_call and no agent run in the flow")
    a = ap.parse_args(argv)
    ns, _, name = a.incident.partition("/")
    events = flow.read(name, ns)
    if not events:
        print(f"no flow for {a.incident}", file=sys.stderr)
        return 2
    s = summarize(latest_run(events))
    print(f"== {a.incident}  ({len(events)} events, latest run)")
    print(f"stages present: {', '.join(s['present']) or '-'}")
    print(f"stages missing: {', '.join(s['missing']) or '-'}")
    print(f"tier: {s['tier']}   route chain: {' -> '.join(s['chain']) or '-'}   playbook: "
          f"{s['route'].get('playbook_id')} ({s['route'].get('playbook_state')})")
    print(f"route reason: {s['route'].get('reason', '-')}")
    ok = sum(1 for c in s["calls"] if c["status"] != "error" and not (c.get("data") or {}).get("error"))
    print(f"tool calls: {len(s['calls'])} ({ok} ok, {len(s['calls']) - ok} error)")
    for c in s["calls"]:
        d = c.get("data") or {}
        bad = c["status"] == "error" or d.get("error")
        print(f"  {'ERR' if bad else 'ok '} {d.get('tool', '?')}")
    if s["extracts"]:
        print(f"extracts: {len(s['extracts'])}")
        for e in s["extracts"]:
            d = e.get("data") or {}
            print(f"  {'ok ' if e['status'] != 'error' else 'ERR'} {d.get('variable')} = {str(d.get('value', d.get('error')))[:60]}")
    print(f"llm: {len(s['llm'])} llm_call, {len(s['agent_runs'])} agent run(s)")
    o = s["outcome"]
    print(f"outcome: {o.get('phase', '-')} ({o.get('reason', '-')})")
    ld = s["learn"]
    print(f"learn: {ld.get('action', '-')} {ld.get('playbook_id') or ''} - {s['learn_title'] or '-'}")
    results = []
    if a.expect_tier:
        results.append((f"tier == {a.expect_tier}", s["tier"] == a.expect_tier, s["tier"]))
    if a.expect_outcome:
        results.append((f"outcome == {a.expect_outcome}", o.get("phase") == a.expect_outcome, o.get("phase")))
    if a.expect_no_llm:
        n = len(s["llm"]) + len(s["agent_runs"])
        results.append(("no LLM", n == 0, f"{n} llm/agent events"))
    for label, good, got in results:
        print(f"{'PASS' if good else 'FAIL'}  {label}  (got {got})")
    if results:
        bad = any(not g for _, g, _ in results)
        print("RESULT: " + ("FAIL" if bad else "PASS"))
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
