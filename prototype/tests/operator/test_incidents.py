from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from repair_operator import incidents
from repair_operator.incidents import in_cooldown, open_incident, set_phase, start_cooldown

FP = "a" * 10 + "b" * 54


class Conflict(Exception):
    status = 409


@pytest.fixture(autouse=True)
def _clean():
    incidents.clear_cooldowns()
    yield
    incidents.clear_cooldowns()


def test_open_incident_creates_named_cr():
    api = MagicMock()
    api.create_namespaced_custom_object.return_value = {"metadata": {"name": "ri-aaaaaaaaaa"}}
    out = open_incident(api, "demo", {"kind": "Pod", "name": "p"}, FP, signature="abc")
    args = api.create_namespaced_custom_object.call_args.args
    assert args[:4] == ("repair.example.dev", "v1alpha1", "demo", "repairincidents")
    body = args[4]
    assert body["metadata"]["name"] == "ri-aaaaaaaaaa"
    assert body["spec"]["fingerprint"] == FP and body["spec"]["source"]["namespace"] == "demo"
    assert out["metadata"]["name"] == "ri-aaaaaaaaaa"


def test_open_incident_409_returns_existing():
    api = MagicMock()
    api.create_namespaced_custom_object.side_effect = Conflict()
    api.get_namespaced_custom_object.return_value = {"existing": True}
    assert open_incident(api, "demo", {}, FP) == {"existing": True}
    api.get_namespaced_custom_object.assert_called_once_with(
        "repair.example.dev", "v1alpha1", "demo", "repairincidents", "ri-aaaaaaaaaa"
    )


def test_open_incident_other_errors_propagate():
    api = MagicMock()
    api.create_namespaced_custom_object.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        open_incident(api, "demo", {}, FP)


def test_cooldown_expires():
    start_cooldown(FP, seconds=600, now=1000.0)
    assert in_cooldown(FP, now=1599.0)
    assert not in_cooldown(FP, now=1600.0)
    assert not in_cooldown(FP, now=1.0)  # entry was dropped on expiry


def test_open_incident_skipped_in_cooldown():
    api = MagicMock()
    start_cooldown(FP, seconds=600)
    assert open_incident(api, "demo", {}, FP) is None
    api.create_namespaced_custom_object.assert_not_called()


def test_set_phase_timestamps():
    now = datetime(2026, 11, 3, 10, 15, 42, tzinfo=UTC)
    patch: dict = {}
    set_phase(patch, "Detected", now=now)
    assert patch["status"]["phase"] == "Detected"
    assert patch["status"]["timestamps"] == {"detected": "2026-11-03T10:15:42Z"}
    set_phase(patch, "Healed", now=now, outcome={"result": "healed"})
    st = patch["status"]
    assert st["timestamps"]["verified"] == st["timestamps"]["closed"] == "2026-11-03T10:15:42Z"
    assert st["outcome"] == {"result": "healed"}


def test_set_phase_analyzing_has_no_timestamp_and_rejects_unknown():
    patch: dict = {}
    set_phase(patch, "Analyzing", attempts=1)
    assert patch["status"] == {"phase": "Analyzing", "attempts": 1}
    with pytest.raises(ValueError):
        set_phase(patch, "Nope")
