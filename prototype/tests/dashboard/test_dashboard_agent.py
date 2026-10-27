import json

import pytest
from fastapi.testclient import TestClient

from repair_dashboard.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REPAIR_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REPAIR_AGENT_PROVIDER", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setattr("shutil.which", lambda _n: None)
    return TestClient(app), tmp_path


def test_config_get_post(client):
    c, d = client
    assert c.get("/api/agent/config").json() == {"provider": "groq", "model": "", "available": {"groq": True, "claude-cli": False}}
    r = c.post("/api/agent/config", json={"provider": "claude-cli", "model": "sonnet"})
    assert r.json()["provider"] == "claude-cli"
    assert json.loads((d / "agent-config.json").read_text()) == {"provider": "claude-cli", "model": "sonnet"}
    assert c.post("/api/agent/config", json={"provider": "x"}).status_code == 400


def test_agent_trace_latest_first(client):
    c, d = client
    (d / "agent-traces").mkdir()
    for n in (1, 2):
        (d / "agent-traces" / f"ri-a-a{n}.json").write_text(json.dumps({"attempt": n, "trace": []}))
    (d / "agent-traces" / "ri-a-b-a9.json").write_text("{}")
    assert [t["attempt"] for t in c.get("/api/incidents/demo/ri-a/agent-trace").json()] == [2, 1]
    assert c.get("/api/incidents/demo/none/agent-trace").json() == []
