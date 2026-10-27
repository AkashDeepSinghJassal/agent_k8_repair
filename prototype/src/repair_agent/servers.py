"""Launch specs for the four stdio MCP servers, shared by both hosts."""

from __future__ import annotations

import os
import sys

SERVERS = ("k8s", "git", "argo", "repair")
PASS_THROUGH = (
    "PATH", "HOME", "KUBECONFIG", "REPAIR_KUBE_CONTEXT", "REPAIR_AGENT_NAMESPACES", "REPAIR_GITHUB_TOKEN",
    "GITHUB_TOKEN", "REPAIR_DEMO_REPO", "REPAIR_AGENT_REPOS", "REPAIR_DEMO_BRANCH", "VIRTUAL_ENV", "PYTHONPATH",
)


def server_specs(out_path: str, env: dict | None = None) -> dict[str, dict]:
    base = {k: os.environ[k] for k in PASS_THROUGH if k in os.environ}
    base.update({k: str(v) for k, v in (env or {}).items()})
    base["REPAIR_AGENT_OUT"] = out_path
    specs = {}
    for name in SERVERS:
        specs[name] = {"command": sys.executable, "args": ["-m", f"repair_mcp.{name}_server"], "env": dict(base)}
    return specs
