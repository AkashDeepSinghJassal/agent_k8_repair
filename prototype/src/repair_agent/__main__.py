"""CLI: python -m repair_agent investigate|tools"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import tempfile
from pathlib import Path


def _seed_from_pod(namespace: str, pod: str, container: str) -> dict:
    seed = {"namespace": namespace, "kind": "Pod", "name": pod, "container": container, "incident": f"cli-{pod}"}
    try:
        import os

        from kubernetes import client, config

        config.load_kube_config(context=os.environ.get("REPAIR_KUBE_CONTEXT", "kind-repair"))
        p = client.CoreV1Api().read_namespaced_pod(pod, namespace)
        refs = p.metadata.owner_references or []
        if refs:
            seed["owner"] = f"{refs[0].kind}/{refs[0].name}"
        statuses = p.status.container_statuses or []
        for cs in statuses:
            if container and cs.name != container:
                continue
            waiting = cs.state.waiting if cs.state else None
            term = (cs.last_state.terminated if cs.last_state else None) or (cs.state.terminated if cs.state else None)
            reason = (waiting.reason if waiting else None) or (term.reason if term else None)
            if reason:
                seed["reason"] = reason
                seed.setdefault("container", cs.name)
                break
        seed["message"] = f"phase={p.status.phase}"
    except Exception as e:  # noqa: BLE001
        seed["message"] = f"(could not inspect pod: {type(e).__name__})"
    return seed


async def _list_tools() -> None:
    from .mcp_tools import McpToolbox
    from .servers import server_specs

    with tempfile.TemporaryDirectory() as tmp:
        async with McpToolbox(server_specs(str(Path(tmp) / "out.json"))) as box:
            for t in box.tools:
                f = t["function"]
                print(f"{f['name']}: {f['description'].splitlines()[0] if f['description'] else ''}")
                print(f"    params: {list(f['parameters'].get('properties', {}))}")
            print(f"{len(box.tools)} tools")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="repair_agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    inv = sub.add_parser("investigate")
    inv.add_argument("--namespace", required=True)
    inv.add_argument("--pod", required=True)
    inv.add_argument("--container", default="")
    inv.add_argument("--provider", choices=["groq", "claude-cli"])
    inv.add_argument("--json", action="store_true")
    sub.add_parser("tools")
    args = ap.parse_args(argv)
    if args.cmd == "tools":
        asyncio.run(_list_tools())
        return 0
    from .runner import investigate

    res = investigate(_seed_from_pod(args.namespace, args.pod, args.container), provider=args.provider)
    if args.json:
        print(json.dumps(dataclasses.asdict(res), indent=2, default=str))
    else:
        for t in res.trace:
            print(f"{t['step']:>2}. {t['tool']} {json.dumps(t['args'])[:100]} ({t['ms']}ms){' ERROR' if t.get('error') else ''}")
        print(f"status: {res.status}  usage: {json.dumps(res.usage)}")
        if res.error:
            print(f"error: {res.error}")
        if res.proposal:
            print(json.dumps(res.proposal, indent=2))
    return 0 if res.status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
