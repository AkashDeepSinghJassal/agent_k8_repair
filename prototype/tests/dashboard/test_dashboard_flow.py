import sys
import types
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from repair_dashboard.app import app
from repair_operator import flow
from repair_operator.config import get_settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REPAIR_STATE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def _write_flow():
    flow.emit("ri-1", "demo", "detected", title="api crashed", data={"fault": "F4"})
    flow.emit("ri-1", "demo", "route", title="T3", tier="T3", data={"chosen": "T3", "reason": "no playbook"})
    s = flow.emit("ri-1", "demo", "agent_start", title="start", tier="T3")
    flow.emit("ri-1", "demo", "tool_call", title="k8s.get_logs", tier="T3", parent=s, data={"tool": "get_logs", "args": {"a": 1}, "result": "x"})
    flow.emit("ri-1", "demo", "agent_end", title="end", tier="T3", parent=s, data={"input_tokens": 100, "output_tokens": 20})
    flow.emit("ri-1", "demo", "outcome", title="Healed", data={"phase": "Healed"})


def test_flow_endpoint(client):
    assert client.get("/api/incidents/demo/none/flow").json() == {"incident": "none", "namespace": "demo", "events": []}
    _write_flow()
    body = client.get("/api/incidents/demo/ri-1/flow").json()
    assert body["incident"] == "ri-1"
    assert [e["kind"] for e in body["events"]][:2] == ["detected", "route"]


def test_recent_flows_summary(client):
    assert client.get("/api/flows/recent").json() == []
    _write_flow()
    (s,) = client.get("/api/flows/recent?limit=5").json()
    assert (s["incident"], s["tier"], s["outcome"], s["steps"], s["tokens"], s["fault"]) == ("ri-1", "T3", "Healed", 1, 120, "F4")
    assert s["start"] <= s["end"] and s["terminal"]


def test_playbooks_empty_without_module(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "repair_operator.playbooks", None)
    assert client.get("/api/playbooks").json() == []
    assert client.get("/api/playbooks/pb-1").status_code == 404


def test_playbooks_from_module(client, monkeypatch):
    @dataclass
    class PB:
        id: str
        state: str
        history: list = field(default_factory=list)

    fake = types.ModuleType("repair_operator.playbooks")
    fake.list_all = lambda: [PB("pb-1", "T2", [{"event": "captured"}])]
    monkeypatch.setitem(sys.modules, "repair_operator.playbooks", fake)
    assert client.get("/api/playbooks").json()[0]["state"] == "T2"
    assert client.get("/api/playbooks/pb-1").json()["history"] == [{"event": "captured"}]
    assert client.get("/api/playbooks/zzz").status_code == 404
