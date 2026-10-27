"""Shared helpers for the read-only MCP servers: env config, allowlists, redaction, lazy clients."""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Callable
from typing import Any

from repair_operator.evidence import MAX_FILE_LINES, MAX_LOG_LINES, redact

DEFAULT_REPO = "AkashDeepSinghJassal/repair-demo-app"
PATH_RE = re.compile(r"^(deploy|workflows)/[\w./-]*\.ya?ml$")
FAILURE_CLASSES = [
    "OOM_KILLED", "IMAGE_PULL", "CONFIG_ERROR", "CRASH_LOOP", "STAGE_FAILURE", "UNSCHEDULABLE", "NETWORK", "OTHER",
]
ARGOCD_NS = "argocd"


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def kube_context() -> str:
    return os.environ.get("REPAIR_KUBE_CONTEXT", "kind-repair")


def allowed_namespaces() -> list[str]:
    return _csv(os.environ.get("REPAIR_AGENT_NAMESPACES", "demo,demo-b"))


def default_repo() -> str:
    return os.environ.get("REPAIR_DEMO_REPO", DEFAULT_REPO)


def allowed_repos() -> list[str]:
    return _csv(os.environ.get("REPAIR_AGENT_REPOS", "")) or [default_repo()]


def default_branch() -> str:
    return os.environ.get("REPAIR_DEMO_BRANCH", "main")


def check_namespace(namespace: str) -> None:
    if namespace not in allowed_namespaces():
        raise ToolError("namespace not allowed")


def check_repo(repo: str) -> str:
    repo = repo or default_repo()
    if repo not in allowed_repos():
        raise ToolError("repo not allowed")
    return repo


def check_path(path: str) -> str:
    if ".." in path.split("/") or not PATH_RE.match(path):
        raise ToolError("path not allowed (must match deploy/** or workflows/** ending in .yaml/.yml)")
    return path


class ToolError(Exception):
    """Expected failure; message is returned to the model as {"error": ...}."""


def truncate_lines(text: str, limit: int, tail: bool = False) -> str:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    kept = lines[-limit:] if tail else lines[:limit]
    note = f"[truncated: showing {'last' if tail else 'first'} {limit} of {len(lines)} lines]"
    return "\n".join(([note] if tail else []) + kept + ([] if tail else [note]))


def safe_text(text: str, limit: int = MAX_FILE_LINES, tail: bool = False) -> str:
    return truncate_lines(redact(text or ""), limit, tail)


def redact_obj(obj: Any) -> Any:
    """Redact every string inside a JSON-like structure."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_obj(o) for o in obj]
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    return obj


def tool(fn: Callable) -> Callable:
    """Never raise to the client: errors become {"error": "..."}."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status", None)
            reason = getattr(exc, "reason", None)
            msg = f"{status} {reason}" if status else f"{type(exc).__name__}: {exc}"
            return {"error": redact(msg)[:500]}

    return wrapper


def plain(obj: Any) -> dict:
    """Kubernetes client model or dict -> camelCase dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    from kubernetes import client

    return client.ApiClient().sanitize_for_serialization(obj) or {}


@functools.lru_cache(maxsize=1)
def _load_kube() -> None:
    from kubernetes import config

    config.load_kube_config(context=kube_context())


def core_api() -> Any:
    from kubernetes import client

    _load_kube()
    return client.CoreV1Api()


def apps_api() -> Any:
    from kubernetes import client

    _load_kube()
    return client.AppsV1Api()


def net_api() -> Any:
    from kubernetes import client

    _load_kube()
    return client.NetworkingV1Api()


def custom_api() -> Any:
    from kubernetes import client

    _load_kube()
    return client.CustomObjectsApi()


@functools.lru_cache(maxsize=8)
def github_repo(repo: str) -> Any:
    from github import Auth, Github

    token = os.environ.get("REPAIR_GITHUB_TOKEN")
    if not token:
        raise ToolError("REPAIR_GITHUB_TOKEN not set")
    return Github(auth=Auth.Token(token)).get_repo(repo)


__all__ = ["MAX_FILE_LINES", "MAX_LOG_LINES"]
