"""Write two fake flow traces (T3 first run of F4, T1 replay of F4) so the dashboard trace view can be shown offline.

    uv run python scripts/demo_flow_fixture.py /tmp/repair-demo-state
    REPAIR_STATE_DIR=/tmp/repair-demo-state uv run uvicorn repair_dashboard.app:app --port 8095

Only flow files are written (plus a best-effort playbook if repair_operator.playbooks is importable).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

NS = "demo"
SIG = "sig-9f3a1c2e7b40"
DIFF = """--- a/deploy/demo/configmaps.yaml
+++ b/deploy/demo/configmaps.yaml
@@ -8,4 +8,4 @@ metadata:
 data:
-  DB_URL: postgres://app:app@db.demo.svc:5432/app
+  DB_URL: postgres://app:app@db-primary.demo.svc:5432/app
   LOG_LEVEL: info
"""
CM = """apiVersion: v1
kind: ConfigMap
metadata:
  name: api-config
  namespace: demo
data:
  DB_URL: postgres://app:app@db-primary.demo.svc:5432/app
  LOG_LEVEL: info
"""
LOGS = (
    "2026-11-04T10:02:11Z api starting\n"
    "2026-11-04T10:02:11Z connecting to postgres://app:***@db-primary.demo.svc:5432/app\n"
    "2026-11-04T10:02:12Z FATAL dial tcp: lookup db-primary.demo.svc: no such host\n"
)
EDIT = [{"find": "db-primary.demo.svc", "replace": "db.demo.svc"}]


class Writer:
    def __init__(self, path: Path, start: datetime, incident: str):
        self.path, self.t, self.incident, self.start = path, start, incident, start
        self.n = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    def emit(self, kind, title, *, after=0.4, data=None, tier=None, parent=None, status="ok", ms=None) -> str:
        self.t += timedelta(seconds=after)
        self.n += 1
        eid = f"{self.incident[-3:]}{self.n:03d}".replace("-", "x")[:12]
        ev = {
            "id": eid, "ts": self.t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{self.t.microsecond // 1000:03d}Z",
            "incident": self.incident, "namespace": NS, "kind": kind, "title": title, "tier": tier,
            "parent": parent, "status": status, "data": data or {},
        }
        if ms is not None:
            ev["duration_ms"] = ms
        with self.path.open("a") as fh:
            fh.write(json.dumps(ev) + "\n")
        return eid


def guardrails(w, tier, parent=None):
    checks = [
        ("allowlisted-file", True, "deploy/demo/configmaps.yaml is under deploy/demo/"),
        ("size-limit", True, "1 edit, 1 line changed (limit 20)"),
        ("find-exists", True, "find text occurs exactly once in the file"),
        ("grounded-values", True, "db.demo.svc appears in the git_diff output"),
        ("no-secrets", True, "no credential-like literal added"),
        ("yaml-valid", True, "result parses as YAML"),
        ("class-matches-evidence", True, "CONFIG_ERROR agrees with the log signature"),
    ]
    for name, ok, detail in checks:
        w.emit("guardrail", f"{'Passed' if ok else 'Rejected'}: {name}", after=0.1, tier=tier, parent=parent,
               data={"name": name, "ok": ok, "detail": detail})


def tail(w, tier, incident, learn):
    w.emit("pr", "Opened pull request #41", after=1.5, tier=tier, ms=1300,
           data={"number": 41, "url": "https://github.com/example/demo-repo/pull/41", "branch": f"repair/{incident}"})
    w.emit("review", "Reviewer merged the pull request", after=14, data={"state": "merged", "feedback": ""})
    w.emit("sync", "Argo CD synced revision 4be91d0", after=6, data={"revision": "4be91d0", "synced": True})
    w.emit("verify", "Healthy for 120 s, verdict: healed", after=121,
           data={"healthySince": "120s", "verdict": "healed", "details": "api 1/1 ready, 0 restarts in window"})
    w.emit("outcome", "Healed", after=0.3, data={"phase": "Healed", "reason": "verified healthy"})
    learn(w)


def tool(w, parent, tier, step, server, name, args, result, ms, error=None):
    w.emit("tool_call", f"{server}.{name}", after=0.9, tier=tier, parent=parent, ms=ms,
           status="error" if error else "ok",
           data={"server": server, "tool": name, "args": args, "result": result, "ms": ms, "error": error, "step": step})


def t3_flow(state: Path, start: datetime) -> str:
    inc = "ri-f4-first"
    w = Writer(state / "flows" / f"{NS}__{inc}.jsonl", start, inc)
    w.emit("detected", "api pod CrashLoopBackOff in demo (CONFIG_ERROR hint)", after=0,
           data={"source": {"kind": "Pod", "name": "api-6d9c7b8f5-x2k4q", "owner": "api", "container": "api",
                            "reason": "CrashLoopBackOff", "classHint": "CONFIG_ERROR"},
                 "fingerprint": SIG, "fault": "F4"})
    w.emit("evidence", "Collected 3 files, 14 events, 120 log lines", after=1.2, ms=1100,
           data={"files": ["deploy/demo/api.yaml", "deploy/demo/configmaps.yaml", "deploy/demo/service.yaml"],
                 "events_count": 14, "log_lines": 120, "configmaps": ["api-config"]})
    w.emit("route", "No playbook for this signature: running the full agent (T3)", tier="T3", after=0.3,
           data={"signature": SIG, "playbook_id": None, "playbook_state": None, "chosen": "T3",
                 "reason": "no playbook for this signature yet; every failure starts as a T3 agent run",
                 "fallback_from": None})
    start_id = w.emit("agent_start", "Agent investigating with claude-cli", tier="T3", after=0.2,
                      data={"provider": "claude-cli", "model": "claude-sonnet-4", "seed": {"namespace": NS, "owner": "api", "container": "api", "file": "deploy/demo/configmaps.yaml"}})
    tool(w, start_id, "T3", 1, "k8s", "list_pods", {"namespace": NS, "label": "app=api"},
         "NAME                    READY  STATUS             RESTARTS\napi-6d9c7b8f5-x2k4q  0/1    CrashLoopBackOff   6\n", 340)
    tool(w, start_id, "T3", 2, "k8s", "get_logs", {"namespace": NS, "pod": "api-6d9c7b8f5-x2k4q", "container": "api", "tail": 120}, LOGS, 410)
    tool(w, start_id, "T3", 3, "k8s", "get_events", {"namespace": NS, "involved": "api-6d9c7b8f5-x2k4q"},
         "Warning  BackOff  Back-off restarting failed container api in pod api-6d9c7b8f5-x2k4q\n", 280)
    tool(w, start_id, "T3", 4, "k8s", "get_configmap", {"namespace": NS, "name": "api-config"},
         json.dumps({"name": "api-config", "data": {"DB_URL": "postgres://app:app@db-primary.demo.svc:5432/app", "LOG_LEVEL": "info"}}, indent=2), 300)
    tool(w, start_id, "T3", 5, "k8s", "get_service", {"namespace": NS, "name": "db-primary"}, "", 250,
         error='services "db-primary" not found')
    tool(w, start_id, "T3", 6, "k8s", "list_services", {"namespace": NS},
         "NAME  TYPE       PORTS\napi   ClusterIP  8080\ndb    ClusterIP  5432\nfrontend  NodePort  80:30080\n", 260)
    tool(w, start_id, "T3", 7, "git", "git_read", {"repo": "demo-repo", "path": "deploy/demo/configmaps.yaml"}, CM, 520)
    tool(w, start_id, "T3", 8, "git", "git_log", {"repo": "demo-repo", "path": "deploy/demo/configmaps.yaml", "n": 3},
         "4be91d0 inject F4: break DB_URL host\n1a77c3e initial demo manifests\n", 380)
    tool(w, start_id, "T3", 9, "git", "git_diff", {"repo": "demo-repo", "path": "deploy/demo/configmaps.yaml", "base": "1a77c3e", "head": "4be91d0"}, DIFF, 450)
    w.emit("agent_end", "Agent finished in 9 steps", tier="T3", parent=start_id, after=6, ms=31000,
           data={"status": "ok", "steps": 9, "input_tokens": 18420, "output_tokens": 1260, "cost_usd": 0.0733, "latency_ms": 31000, "fallback_from": None})
    p = w.emit("proposal", "Revert DB_URL host to db.demo.svc", tier="T3", after=0.3,
               data={"tier": "T3", "class": "CONFIG_ERROR", "root_cause": "DB_URL points at db-primary, which does not exist; the git_diff shows it was db.demo.svc",
                     "confidence": 0.93, "file": "deploy/demo/configmaps.yaml", "edits": EDIT,
                     "rationale": "Restore the host removed by the last commit (seen on the '-' line of git_diff)."})
    guardrails(w, "T3", p)

    def learn(w):
        w.emit("learn", "Captured playbook pb-4f3a91 from this run", after=0.4, tier="T3",
               data={"action": "captured", "playbook_id": "pb-4f3a91", "from_state": None, "to_state": "T3",
                     "counts": {"t3_ok": 1}, "reason": "Healed and merged change equals the proposal; DB_URL value extracted from git_diff"})

    tail(w, "T3", inc, learn)
    return inc


def t1_flow(state: Path, start: datetime) -> str:
    inc = "ri-f4-replay"
    w = Writer(state / "flows" / f"{NS}__{inc}.jsonl", start, inc)
    w.emit("detected", "api pod CrashLoopBackOff in demo (CONFIG_ERROR hint)", after=0,
           data={"source": {"kind": "Pod", "name": "api-6d9c7b8f5-m8w2z", "owner": "api", "container": "api",
                            "reason": "CrashLoopBackOff", "classHint": "CONFIG_ERROR"},
                 "fingerprint": SIG, "fault": "F4"})
    w.emit("evidence", "Collected 3 files, 11 events, 120 log lines", after=1.1, ms=1000,
           data={"files": ["deploy/demo/api.yaml", "deploy/demo/configmaps.yaml"], "events_count": 11, "log_lines": 120, "configmaps": ["api-config"]})
    r = w.emit("route", "Playbook pb-4f3a91 is promoted to T1: replaying without an LLM", tier="T1", after=0.2,
               data={"signature": SIG, "playbook_id": "pb-4f3a91", "playbook_state": "T1", "chosen": "T1",
                     "reason": "promoted to T1 after 2 verified hybrid runs whose LLM values equalled the extract rule",
                     "fallback_from": None})
    tool(w, r, "T1", 1, "k8s", "get_logs", {"namespace": NS, "pod": "${pod}", "container": "api", "tail": 120}, LOGS, 190)
    tool(w, r, "T1", 2, "git", "git_read", {"repo": "demo-repo", "path": "deploy/demo/configmaps.yaml"}, CM, 240)
    tool(w, r, "T1", 3, "git", "git_diff", {"repo": "demo-repo", "path": "deploy/demo/configmaps.yaml", "base": "HEAD~1", "head": "HEAD"}, DIFF, 260)
    w.emit("extract", "DB_URL host = db.demo.svc", after=0.1, tier="T1", parent=r,
           data={"variable": "good_host", "from_step": 3, "rule": {"type": "diff_removed_line", "key": "DB_URL"},
                 "value": "postgres://app:app@db.demo.svc:5432/app"})
    p = w.emit("proposal", "Rendered edit from playbook: restore DB_URL", tier="T1", after=0.1,
               data={"tier": "T1", "class": "CONFIG_ERROR", "root_cause": "DB_URL host changed; value taken from the removed line of git_diff (no LLM)",
                     "confidence": 0.95, "file": "deploy/demo/configmaps.yaml", "edits": EDIT,
                     "rationale": "Deterministic replay of playbook pb-4f3a91."})
    guardrails(w, "T1", p)

    def learn(w):
        w.emit("learn", "Promoted pb-4f3a91 T3 -> T1 (3 verified runs)", after=0.4, tier="T1",
               data={"action": "promoted", "playbook_id": "pb-4f3a91", "from_state": "T3", "to_state": "T1",
                     "counts": {"t3_ok": 1, "t2_ok": 2, "t1_ok": 1}, "reason": "t1_ok reached threshold; no t1_blocked"})

    tail(w, "T1", inc, learn)
    return inc


def seed_playbook() -> None:
    try:
        from repair_operator import playbooks as pb
        now = datetime.now(UTC).isoformat()
        play = pb.Playbook(
            id="pb-4f3a91", signature=SIG, cls="CONFIG_ERROR", state="T1",
            steps=[pb.Step("k8s", "get_logs", {"namespace": "${namespace}", "pod": "${pod}", "container": "${container}"}),
                   pb.Step("git", "git_read", {"repo": "${repo}", "path": "${file}"}),
                   pb.Step("git", "git_diff", {"repo": "${repo}", "path": "${file}", "base": "HEAD~1", "head": "HEAD"})],
            extracts=[pb.Extract("good_host", 2, {"type": "diff_removed_line", "key": "DB_URL"})],
            file_template="${file}", edits_template=[{"find": "${bad}", "replace": "${good_host}"}],
            action_sequence=["get_logs", "git_read", "git_diff"],
            runs={"t3_ok": 1, "t2_ok": 2, "t1_ok": 1, "fail": 0, "edited": 0}, consistency=1.0,
            history=[{"ts": now, "event": "captured", "from": None, "to": "T3", "reason": "first healed run", "incident": "ri-f4-first"},
                     {"ts": now, "event": "promoted", "from": "T3", "to": "T1", "reason": "3 verified runs", "incident": "ri-f4-replay"}],
            created=now, updated=now)
        pb.upsert(play)
        print("seeded playbook pb-4f3a91")
    except Exception as exc:  # noqa: BLE001
        print(f"playbook not seeded ({type(exc).__name__}: {exc})")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: demo_flow_fixture.py <state_dir>")
    state = Path(sys.argv[1]).expanduser()
    import os
    os.environ["REPAIR_STATE_DIR"] = str(state)
    now = datetime.now(UTC)
    a = t3_flow(state, now - timedelta(minutes=12))
    b = t1_flow(state, now - timedelta(minutes=4))
    print(f"wrote flows {a} and {b} in {state}/flows")
    seed_playbook()
    print(f"REPAIR_STATE_DIR={state} uv run uvicorn repair_dashboard.app:app --port 8095")


if __name__ == "__main__":
    main()
