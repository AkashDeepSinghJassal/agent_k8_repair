"""Agent host that runs the local ``claude -p`` CLI against the same MCP servers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .config import model_for
from .prompt import SYSTEM_PROMPT, render_seed
from .result import AgentResult, files_from_trace, preview, read_outcome, short_tool
from .servers import SERVERS, server_specs


def find_claude() -> str | None:
    return shutil.which("claude") or (str(p) if (p := Path.home() / ".local/bin/claude").exists() else None)


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return json.dumps(content, default=str)


class StreamParser:
    """Feed stream-json lines; accumulates trace, evidence and the final result event."""

    def __init__(self) -> None:
        self.trace: list[dict] = []
        self.evidence: list[str] = []
        self.result: dict | None = None
        self.assistant_turns = 0
        self._pending: dict[str, tuple[dict, float]] = {}

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except ValueError:
            return
        if not isinstance(ev, dict):
            return
        kind = ev.get("type")
        if kind == "result":
            self.result = ev
            return
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        if kind == "assistant":
            self.assistant_turns += 1
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                entry = {"step": len(self.trace) + 1, "tool": short_tool(b.get("name", "")), "args": b.get("input") or {},
                         "result_preview": "", "ms": 0}
                self.trace.append(entry)
                self._pending[b.get("id", "")] = (entry, time.monotonic())
            elif b.get("type") == "tool_result":
                text = _block_text(b.get("content"))
                entry, t0 = self._pending.pop(b.get("tool_use_id", ""), (None, 0.0))
                self.evidence.append(text)
                if entry is not None:
                    entry["result_preview"] = preview(text)
                    entry["ms"] = int((time.monotonic() - t0) * 1000)
                    if b.get("is_error"):
                        entry["error"] = True


def build_command(claude: str, cfg_path: str, model: str, max_turns: int = 40) -> list[str]:
    return [
        claude, "-p", "--mcp-config", cfg_path, "--strict-mcp-config",
        "--tools", "", "--allowedTools", *[f"mcp__{s}" for s in SERVERS],
        "--permission-mode", "dontAsk", "--output-format", "stream-json", "--verbose",
        "--max-turns", str(max_turns), "--model", model, "--append-system-prompt", SYSTEM_PROMPT,
    ]


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env.pop("CLAUDECODE", None)
    return env


def build_result(parser: StreamParser, out_path: str, model: str, started: float, error: str = "") -> AgentResult:
    res = parser.result or {}
    u = res.get("usage") or {}
    usage = {
        "provider": "claude-cli", "model": model, "steps": res.get("num_turns") or parser.assistant_turns,
        "tool_calls": len(parser.trace),
        "input_tokens": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
        "output_tokens": u.get("output_tokens") or 0, "latency_ms": int((time.monotonic() - started) * 1000),
    }
    if res.get("total_cost_usd") is not None:
        usage["cost_usd"] = res["total_cost_usd"]
    outcome = read_outcome(out_path)
    if outcome is not None:
        status, error = outcome["status"], ""
    else:
        status = "failed"
        if not error:
            error = (str(res.get("result") or "") if res.get("is_error") else "") or "agent finished without propose_patch/escalate"
    return AgentResult(status, outcome, parser.trace, "\n".join(parser.evidence), files_from_trace(parser.trace), usage, error[:500])


def _run_once(cmd: list[str], prompt: str, cwd: str, parser: StreamParser, timeout_s: float) -> tuple[int | None, str]:
    """Returns (returncode or None on timeout, stderr tail)."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            cwd=cwd, env=child_env())
    err: list[str] = []
    t = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)  # type: ignore[union-attr]
    t.start()
    timed_out = threading.Event()

    def kill() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout_s, kill)
    timer.start()
    try:
        proc.stdin.write(prompt)  # type: ignore[union-attr]
        proc.stdin.close()  # type: ignore[union-attr]
        for line in proc.stdout:  # type: ignore[union-attr]
            parser.feed(line)
        proc.wait()
    finally:
        timer.cancel()
    t.join(2)
    stderr = (err[0] if err else "")[-400:]
    return (None if timed_out.is_set() else proc.returncode), stderr


def run_claude(seed: dict, feedback: list[str] | None, *, max_steps: int, timeout_s: int, model: str | None = None) -> AgentResult:
    model = model or model_for("claude-cli")
    started = time.monotonic()
    claude = find_claude()
    if claude is None:
        return AgentResult("failed", usage={"provider": "claude-cli", "model": model}, error="claude CLI not found")
    try:
        with tempfile.TemporaryDirectory(prefix="repair-agent-claude-") as tmp:
            out = str(Path(tmp) / "out.json")
            cfg = Path(tmp) / "mcp.json"
            cfg.write_text(json.dumps({"mcpServers": server_specs(out)}))
            cwd = Path(tmp) / "cwd"
            cwd.mkdir()
            cmd = build_command(claude, str(cfg), model, max_turns=max(40, max_steps + 10))
            prompt = render_seed(seed, feedback)
            parser = StreamParser()
            error = ""
            for attempt in (1, 2):
                parser = StreamParser()
                remaining = max(30.0, timeout_s - (time.monotonic() - started))
                rc, stderr = _run_once(cmd, prompt, str(cwd), parser, remaining)
                if rc is None:
                    error = "timeout"
                    break
                if read_outcome(out) is not None or rc == 0:
                    error = ""
                    break
                error = f"claude exited {rc}: {stderr}".strip()
                if time.monotonic() - started > timeout_s:
                    break
            return build_result(parser, out, model, started, error)
    except Exception as e:  # noqa: BLE001
        return AgentResult("failed", usage={"provider": "claude-cli", "model": model}, error=f"{type(e).__name__}: {str(e)[:300]}")
