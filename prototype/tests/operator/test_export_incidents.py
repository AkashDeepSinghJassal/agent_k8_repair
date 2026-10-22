import csv
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("export_incidents", ROOT / "scripts" / "export_incidents.py")
export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export)

FULL = {
    "metadata": {"name": "ri-aaaaaaaaaa", "namespace": "demo"},
    "spec": {"source": {"kind": "Pod", "name": "api"}, "fingerprint": "f" * 64},
    "status": {
        "phase": "Healed",
        "attempts": 2,
        "classification": {"class": "OOM_KILLED", "confidence": 0.92, "source": "rules"},
        "proposal": {"tier": 1, "file": "deploy/demo/api.yaml"},
        "outcome": {"result": "healed"},
        "timestamps": {
            "detected": "2026-11-03T10:00:00Z",
            "collected": "2026-11-03T10:01:00Z",
            "analyzed": "2026-11-03T10:01:30Z",
            "proposed": "2026-11-03T10:01:30Z",
            "prOpened": "2026-11-03T10:02:00Z",
            "merged": "2026-11-03T10:12:00Z",
            "synced": "2026-11-03T10:12:30Z",
            "verified": "2026-11-03T10:14:30Z",
            "closed": "2026-11-03T10:14:30Z",
        },
    },
}

PARTIAL = {
    "metadata": {"name": "ri-bbbbbbbbbb", "namespace": "demo-b"},
    "spec": {"source": {"kind": "Pod"}, "fingerprint": "e" * 64},
    "status": {
        "phase": "AwaitingReview",
        "classification": {"class": "CONFIG_ERROR", "confidence": 0.5, "source": "llm"},
        "proposal": {"tier": 2, "rejectedBy": "policy"},
        "timestamps": {"detected": "2026-11-03T10:00:00Z", "prOpened": "2026-11-03T10:00:45Z"},
    },
}


@pytest.fixture
def dump(tmp_path):
    p = tmp_path / "dump.json"
    p.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": [FULL, PARTIAL]}))
    return p


def test_csv_columns_and_durations_from_dump(dump, tmp_path):
    out = tmp_path / "incidents.csv"
    assert export.main(["--from-file", str(dump), "--out", str(out)]) == 0
    with out.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == export.COLUMNS
    full = rows[0]

    assert full["name"] == "ri-aaaaaaaaaa" and full["ns"] == "demo"
    assert full["class"] == "OOM_KILLED" and full["confidence"] == "0.92"
    assert full["tier"] == "1" and full["source"] == "rules"
    assert full["attempts"] == "2" and full["outcome"] == "healed"
    assert full["rejectedBy"] == ""
    assert full["detected_to_collected_s"] == "60"
    assert full["collected_to_analyzed_s"] == "30"
    assert full["analyzed_to_prOpened_s"] == "30"
    assert full["prOpened_to_merged_s"] == "600"
    assert full["merged_to_synced_s"] == "30"
    assert full["synced_to_verified_s"] == "120"
    assert full["detected_to_prOpened_s"] == "120"


def test_missing_timestamps_leave_cells_empty(dump, tmp_path):
    out = tmp_path / "incidents.csv"
    export.main(["--from-file", str(dump), "--out", str(out)])
    rows = list(csv.DictReader(out.open(newline="")))
    partial = rows[1]
    assert partial["rejectedBy"] == "policy" and partial["outcome"] == ""
    assert partial["attempts"] == ""
    assert partial["detected_to_prOpened_s"] == "45"
    for col in (
        "detected_to_collected_s",
        "collected_to_analyzed_s",
        "prOpened_to_merged_s",
        "merged_to_synced_s",
        "synced_to_verified_s",
    ):
        assert partial[col] == ""


def test_single_object_and_bare_list_dumps(tmp_path):
    out = tmp_path / "x.csv"
    single = tmp_path / "single.json"
    single.write_text(json.dumps(FULL))
    assert export.main(["--from-file", str(single), "--out", str(out)]) == 0
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps([FULL, PARTIAL]))
    assert export.main(["--from-file", str(bare), "--out", str(out)]) == 0
    assert len(list(csv.DictReader(out.open(newline="")))) == 2


def test_empty_dump_writes_header_only(tmp_path):
    src = tmp_path / "empty.json"
    src.write_text(json.dumps({"items": []}))
    out = tmp_path / "out" / "incidents.csv"
    export.main(["--from-file", str(src), "--out", str(out)])
    assert out.read_text().strip() == ",".join(export.COLUMNS)
