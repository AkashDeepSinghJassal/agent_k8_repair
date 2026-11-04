"""Hard wall-clock deadline for the claude host (fake `claude` that hangs, with a child holding the pipes)."""

from __future__ import annotations

import os
import stat
import time

from repair_agent import runner


def test_claude_host_timeout_kills_group(tmp_path, monkeypatch):
    marker = tmp_path / "child.pid"
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\n(sleep 300 & echo $! > {marker}; wait) &\nsleep 300\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("REPAIR_AGENT_FALLBACK", "0")
    t0 = time.monotonic()
    res = runner.investigate({"incident": {}}, provider="claude-cli", timeout_s=2)
    assert time.monotonic() - t0 < 12
    assert res.status == "failed"
    assert "timeout after 2s" in res.error
    pid = int(marker.read_text())
    time.sleep(0.3)
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive
