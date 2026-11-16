"""Run the same injected faults through several agent providers in parallel.

    uv run python scripts/model_bench.py --faults F1,F4 --providers claude-cli,scads-high,scads-medium,scads-low

Per fault: inject (python -m repair_faults inject, needs REPAIR_GITHUB_TOKEN), wait for a failing pod, run
``python -m repair_agent investigate --no-fallback --out ...`` for every provider at once, revert, wait for health.
The agent's propose_patch only writes a local file; no PR is opened and the operator is not touched.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONTEXT = "kind-repair"
FAILING_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError"}


def failing_pod(pods: list[dict]) -> tuple[str, str] | None:
    """(pod name, reason) of the first pod showing one of the fault symptoms, else None. Pure, takes pod JSON dicts."""
    for pod in pods:
        name = pod["metadata"]["name"]
        status = pod.get("status") or {}
        for cs in status.get("containerStatuses") or []:
            waiting = (cs.get("state") or {}).get("waiting") or {}
            term = (cs.get("lastState") or {}).get("terminated") or {}
            if waiting.get("reason") in FAILING_WAITING:
                return name, waiting["reason"]
            if term.get("reason") == "OOMKilled":
                return name, "OOMKilled"
            if cs.get("restartCount", 0) >= 3 and term:
                return name, "LivenessProbeRestarts"
        if status.get("phase") == "Pending":
            for cond in status.get("conditions") or []:
                if cond.get("type") == "PodScheduled" and cond.get("status") == "False" and cond.get("reason") == "Unschedulable":
                    return name, "FailedScheduling"
        if status.get("phase") == "Failed":
            return name, "Failed"
    return None


def pods_healthy(pods: list[dict]) -> bool:
    if not pods:
        return False
    for pod in pods:
        status = pod.get("status") or {}
        if status.get("phase") in ("Succeeded",):
            continue
        if (pod["metadata"].get("deletionTimestamp")) or status.get("phase") != "Running":
            return False
        if not all(cs.get("ready") for cs in status.get("containerStatuses") or []):
            return False
    return True


def kubectl_json(args: list[str]) -> Any:
    out = subprocess.run(["kubectl", "--context", CONTEXT, *args, "-o", "json"], check=False, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[:200])
    return json.loads(out.stdout)


def namespace_pods(ns: str) -> list[dict]:
    return kubectl_json(["-n", ns, "get", "pods"])["items"]


def argo_ok(app: str) -> bool:
    try:
        st = kubectl_json(["-n", "argocd", "get", "application", app]).get("status", {})
    except Exception:  # noqa: BLE001
        return False
    return (st.get("sync") or {}).get("status") == "Synced" and (st.get("health") or {}).get("status") == "Healthy"


def wait_for(cond: Callable[[], Any], timeout: float, interval: float = 5, sleep: Callable[[float], None] = time.sleep) -> Any:
    end = time.monotonic() + timeout
    while True:
        try:
            val = cond()
        except Exception:  # noqa: BLE001
            val = None
        if val:
            return val
        if time.monotonic() >= end:
            return None
        sleep(interval)


def faults_cli(action: str, fault: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "repair_faults", action, fault], check=False, capture_output=True, text=True, cwd=ROOT, timeout=600)


def run_provider(fault: str, ns: str, pod: str, container: str, provider: str, out_file: Path, timeout: int) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "repair_agent", "investigate", "--namespace", ns, "--pod", pod, "--provider", provider,
           "--no-fallback", "--timeout", str(timeout), "--out", str(out_file)]
    if container:
        cmd += ["--container", container]
    return subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def summarize(path: Path) -> dict:
    """One summary row from an AgentResult JSON file (never includes secrets: the files hold none)."""
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"status": "no_result", "error": "no result file written"}
    u = d.get("usage") or {}
    prop = d.get("proposal") or {}
    row = {"status": d.get("status"), "tool_calls": u.get("tool_calls", len(d.get("trace") or [])),
           "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
           "latency_ms": d.get("latency_ms") or u.get("latency_ms"), "model": d.get("model") or u.get("model"),
           "error": d.get("error") or ""}
    if d.get("status") == "proposed":
        row.update(file=prop.get("file"), edits=prop.get("edits"), root_cause=prop.get("root_cause"))
    elif d.get("status") == "escalated":
        row.update(escalation=prop.get("reason") or prop, root_cause=prop.get("root_cause"))
    return row


def update_stopped(stopped: dict[str, str], rows: dict[str, dict], fault: str) -> None:
    """Providers that returned rate_limited are stopped for all remaining faults."""
    for prov, row in rows.items():
        if row.get("status") == "rate_limited" and prov not in stopped:
            stopped[prov] = f"rate_limited at {fault}: {row.get('error', '')[:200]}"


def active_providers(providers: list[str], stopped: dict[str, str]) -> list[str]:
    return [p for p in providers if p not in stopped]


def bench_fault(fault: str, providers: list[str], out_dir: Path, args: argparse.Namespace, stopped: dict[str, str]) -> dict:
    from repair_faults.injector import ARGO_APP_BY_NAMESPACE, get_fault

    entry = get_fault(fault)
    ns = entry["namespace"]
    fdir = out_dir / fault
    fdir.mkdir(parents=True, exist_ok=True)
    res: dict[str, Any] = {"providers": {}}
    inj = faults_cli("inject", fault)
    if inj.returncode != 0:
        res["error"] = f"inject failed: {inj.stderr[:300]}"
        return res
    try:
        found = wait_for(lambda: failing_pod(namespace_pods(ns)), args.wait_symptom)
        if not found:
            res["error"] = f"no failing pod within {args.wait_symptom}s"
        else:
            pod, reason = found
            res["pod"], res["symptom"] = pod, reason
            container = ""
            runners = {p: run_provider(fault, ns, pod, container, p, fdir / f"{p.replace(':', '_').replace('/', '_')}.json", args.timeout)
                       for p in active_providers(providers, stopped)}
            for p, proc in runners.items():
                try:
                    proc.wait(timeout=args.timeout + 120)
                except subprocess.TimeoutExpired:
                    proc.kill()
                res["providers"][p] = summarize(fdir / f"{p.replace(':', '_').replace('/', '_')}.json")
    finally:
        rev = faults_cli("revert", fault)
        if rev.returncode != 0:
            res["revert_error"] = rev.stderr[:300]
        app = entry.get("argo_app") or ARGO_APP_BY_NAMESPACE.get(ns, "demo-app")
        ok = wait_for(lambda: argo_ok(app) and pods_healthy(namespace_pods(ns)), 600, 10)
        res["recovered"] = bool(ok)
    for p in providers:
        if p in stopped and p not in res["providers"]:
            res["providers"][p] = {"status": "skipped", "error": stopped[p]}
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--faults", required=True)
    ap.add_argument("--providers", default="claude-cli,scads-high,scads-medium,scads-low")
    ap.add_argument("--out", default="")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--wait-symptom", type=int, default=300)
    args = ap.parse_args(argv)
    out_dir = Path(args.out or ROOT / "results" / "model-bench" / datetime.now().strftime("%Y%m%d-%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    summary: dict[str, Any] = {"providers": providers, "faults": {}, "stopped": {}}
    stopped: dict[str, str] = summary["stopped"]
    for fault in [f.strip() for f in args.faults.split(",") if f.strip()]:
        if not active_providers(providers, stopped):
            summary["faults"][fault] = {"error": "all providers stopped"}
            continue
        print(f"== {fault}: {active_providers(providers, stopped)}", flush=True)
        r = bench_fault(fault, providers, out_dir, args, stopped)
        update_stopped(stopped, r["providers"], fault)
        summary["faults"][fault] = r
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        for p, row in r["providers"].items():
            print(f"   {p}: {row.get('status')} tools={row.get('tool_calls')} {row.get('latency_ms')}ms", flush=True)
        if not r.get("recovered", True):
            print("   cluster did not recover; stopping", file=sys.stderr)
            break
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"summary: {out_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
