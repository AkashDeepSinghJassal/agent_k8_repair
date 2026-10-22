import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from repair_operator import incidents, metrics
from repair_operator.models import Phase


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setattr(metrics, "get_settings", lambda: SimpleNamespace(state_dir=d))
    return d


def test_emit_writes_valid_json_line_to_stdout_and_file(state_dir, capsys):
    metrics.emit("ri-aaaaaaaaaa", "demo", Phase.DETECTED, tier=None, attempt=0)
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    event = json.loads(out[0])
    assert event["incident"] == "ri-aaaaaaaaaa"
    assert event["namespace"] == "demo"
    assert event["phase"] == "Detected"
    assert event["attempt"] == 0
    assert "tier" not in event  # None values are dropped
    assert event["ts"].endswith("Z")

    lines = (state_dir / "events.jsonl").read_text().splitlines()
    assert [json.loads(x) for x in lines] == [event]


def test_emit_appends(state_dir):
    metrics.emit("ri-1", "demo", "Collected")
    metrics.emit("ri-1", "demo", "Analyzing", **{"class": "OOM_KILLED"})
    rows = [json.loads(x) for x in (state_dir / "events.jsonl").read_text().splitlines()]
    assert [r["phase"] for r in rows] == ["Collected", "Analyzing"]
    assert rows[1]["class"] == "OOM_KILLED"


def test_emit_never_raises_on_io_error(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")  # state dir cannot be created under a regular file
    monkeypatch.setattr(metrics, "get_settings", lambda: SimpleNamespace(state_dir=blocker / "sub"))
    event = metrics.emit("ri-1", "demo", "Detected")
    assert event["phase"] == "Detected"
    assert json.loads(capsys.readouterr().out)["incident"] == "ri-1"


def test_emit_never_raises_when_settings_fail(monkeypatch):
    def boom():
        raise RuntimeError("bad settings")

    monkeypatch.setattr(metrics, "get_settings", boom)
    assert metrics.emit("ri-1", None, "Detected")["phase"] == "Detected"


def test_set_phase_emits_when_incident_known(state_dir, capsys):
    body = {"metadata": {"name": "ri-bbbbbbbbbb", "namespace": "demo"}}
    patch: dict = {}
    now = datetime(2026, 11, 3, 10, 0, 0, tzinfo=UTC)
    incidents.set_phase(patch, "Detected", now=now, incident=body)
    incidents.set_phase(
        patch,
        "Analyzing",
        incident=body,
        attempts=1,
        classification={"class": "OOM_KILLED", "source": "rules", "confidence": 0.9},
        proposal={"tier": 1},
    )
    events = [json.loads(x) for x in (state_dir / "events.jsonl").read_text().splitlines()]
    assert [e["phase"] for e in events] == ["Detected", "Analyzing"]
    assert events[1]["incident"] == "ri-bbbbbbbbbb" and events[1]["namespace"] == "demo"
    assert events[1]["class"] == "OOM_KILLED"
    assert events[1]["tier"] == 1
    assert events[1]["source"] == "rules"
    assert events[1]["attempt"] == 1
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_set_phase_without_incident_emits_nothing(state_dir, capsys):
    patch: dict = {}
    incidents.set_phase(patch, "Detected")
    incidents.set_phase(patch, "Analyzing", attempts=1)
    assert capsys.readouterr().out == ""
    assert not (state_dir / "events.jsonl").exists()
    assert patch["status"]["phase"] == "Analyzing"


def test_set_phase_accepts_name_string(state_dir):
    patch: dict = {}
    incidents.set_phase(patch, "Merged", incident="ri-cccccccccc", namespace="demo-b")
    event = json.loads((state_dir / "events.jsonl").read_text())
    assert event["incident"] == "ri-cccccccccc" and event["namespace"] == "demo-b"
