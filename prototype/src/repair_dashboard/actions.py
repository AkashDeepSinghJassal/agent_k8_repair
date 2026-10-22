"""Server-side write actions: GitHub PR operations and fault state. The token never leaves the server."""

from __future__ import annotations

import os
import re
import subprocess
import threading
from datetime import UTC, datetime
from typing import Any, Protocol

DEFAULT_REPO = "AkashDeepSinghJassal/repair-demo-app"
TOKEN_MESSAGE = "REPAIR_GITHUB_TOKEN is not set; GitHub actions are disabled. Export it and restart the dashboard."


class GitHubUnavailable(Exception):
    """No token configured, or GitHub could not be reached."""


class PRClient(Protocol):
    def diff(self, number: int) -> list[dict]: ...

    def merge_squash(self, number: int) -> str: ...

    def comment(self, number: int, text: str) -> None: ...


def demo_repo() -> str:
    return os.environ.get("REPAIR_DEMO_REPO", DEFAULT_REPO)


def pr_number(url: str | None, repo: str | None = None) -> int:
    """Parse a PR URL of the configured repo. Raises ValueError otherwise."""
    m = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)/?", (url or "").strip())
    if not m:
        raise ValueError("incident has no usable pull request URL")
    if m.group(1).lower() != (repo or demo_repo()).lower():
        raise ValueError(f"pull request belongs to {m.group(1)}, not the demo repo")
    return int(m.group(2))


def approver() -> str:
    name = os.environ.get("REPAIR_APPROVER")
    if name:
        return name
    try:
        proc = subprocess.run(["git", "config", "user.name"], check=False, capture_output=True, text=True, timeout=3)
        out = proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    return out or "human"


class GitHubPRs:
    """PyGithub-backed PR client. Raises GitHubUnavailable when no token is set."""

    def __init__(self, token: str | None = None, repo: str | None = None) -> None:
        self._token = token
        self._repo_name = repo
        self._repo = None

    def _get(self):
        token = self._token or os.environ.get("REPAIR_GITHUB_TOKEN")
        if not token:
            raise GitHubUnavailable(TOKEN_MESSAGE)
        if self._repo is None:
            from github import Auth, Github

            self._repo = Github(auth=Auth.Token(token)).get_repo(self._repo_name or demo_repo())
        return self._repo

    @staticmethod
    def _wrap(fn):
        try:
            return fn()
        except GitHubUnavailable:
            raise
        except Exception as exc:
            raise GitHubUnavailable(f"GitHub request failed: {str(exc)[:200]}") from exc

    def diff(self, number):
        def go():
            pr = self._get().get_pull(number)
            return [
                {
                    "filename": f.filename,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                    "patch": f.patch or "",
                }
                for f in pr.get_files()
            ]

        return self._wrap(go)

    def merge_squash(self, number):
        def go():
            res = self._get().get_pull(number).merge(merge_method="squash")
            if not res.merged:
                raise RuntimeError(res.message or "merge refused")
            return res.sha

        return self._wrap(go)

    def comment(self, number, text):
        self._wrap(lambda: self._get().get_pull(number).create_issue_comment(text))


class FaultState:
    """In-memory record of faults injected through this dashboard process."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.injected: dict[str, str] = {}

    def mark(self, fault_id: str) -> str:
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.injected[fault_id] = ts
        return ts

    def clear(self, fault_id: str) -> None:
        self.injected.pop(fault_id, None)


class FaultBackend:
    """Clients handed to repair_faults.injector. None means the injector's defaults (env token, kubectl)."""

    def __init__(self, git: Any = None, kubectl: Any = None, state: FaultState | None = None) -> None:
        self.git, self.kubectl = git, kubectl
        self.state = state or FaultState()
