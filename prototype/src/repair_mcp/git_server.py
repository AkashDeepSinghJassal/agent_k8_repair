"""Read-only GitHub MCP server ("git"). Paths limited to deploy/** and workflows/** yaml."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from . import common as c

MAX_TREE = 300


def _repo(repo: str, client: Any) -> Any:
    name = c.check_repo(repo)
    return client if client is not None else c.github_repo(name)


@c.tool
def git_list(prefix: str = "deploy/", repo: str = "", *, client: Any = None) -> Any:
    r = _repo(repo, client)
    if ".." in prefix.split("/") or not prefix.startswith(("deploy", "workflows")):
        raise c.ToolError("prefix not allowed (deploy/ or workflows/)")
    tree = r.get_git_tree(c.default_branch(), recursive=True).tree
    paths = sorted(
        t.path for t in tree if t.type == "blob" and t.path.startswith(prefix) and c.PATH_RE.match(t.path)
    )
    return paths[:MAX_TREE]


@c.tool
def git_read(path: str, ref: str = "", repo: str = "", *, client: Any = None) -> Any:
    c.check_path(path)
    r = _repo(repo, client)
    f = r.get_contents(path, ref=ref or c.default_branch())
    if isinstance(f, list):
        raise c.ToolError("path is a directory")
    return c.safe_text(f.decoded_content.decode("utf-8", "replace"))


@c.tool
def git_log(path: str, limit: int = 5, repo: str = "", *, client: Any = None) -> Any:
    c.check_path(path)
    r = _repo(repo, client)
    limit = max(1, min(int(limit), 20))
    out = []
    for commit in r.get_commits(path=path, sha=c.default_branch())[:limit]:
        git = commit.commit
        out.append({
            "sha": commit.sha,
            "date": git.author.date.isoformat() if git.author and git.author.date else "",
            "message": c.redact((git.message or "").splitlines()[0] if git.message else ""),
        })
    return out


@c.tool
def git_diff(path: str, sha: str = "", repo: str = "", *, client: Any = None) -> Any:
    c.check_path(path)
    r = _repo(repo, client)
    commit = r.get_commit(sha) if sha else r.get_commits(path=path, sha=c.default_branch())[0]
    parts = [
        f"--- a/{path}\n+++ b/{path}\n{f.patch}"
        for f in commit.files
        if f.filename == path and getattr(f, "patch", None)
    ]
    if not parts:
        return f"(commit {commit.sha[:8]} has no diff for {path})"
    return f"commit {commit.sha}\n" + c.safe_text("\n".join(parts))


def build_server(client: Any = None) -> MCPServer:
    srv = MCPServer("git")

    @srv.tool(name="git_list", description="List yaml files in the GitOps repo under prefix (deploy/ or workflows/). repo defaults to the demo repo.")
    def git_list_(prefix: str = "deploy/", repo: str = "") -> Any:
        return git_list(prefix, repo, client=client)

    @srv.tool(name="git_read", description="Read a deploy/** or workflows/** yaml file (first 400 lines, redacted) at ref (default: main).")
    def git_read_(path: str, ref: str = "", repo: str = "") -> Any:
        return git_read(path, ref, repo, client=client)

    @srv.tool(name="git_log", description="Recent commits touching a path: [{sha, date, message}].")
    def git_log_(path: str, limit: int = 5, repo: str = "") -> Any:
        return git_log(path, limit, repo, client=client)

    @srv.tool(name="git_diff", description="Unified diff of one file in commit sha (default: last commit touching it).")
    def git_diff_(path: str, sha: str = "", repo: str = "") -> Any:
        return git_diff(path, sha, repo, client=client)

    return srv


if __name__ == "__main__":
    build_server().run()
