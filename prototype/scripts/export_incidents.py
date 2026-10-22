"""Export RepairIncidents to CSV: one row per incident, per-stage durations in seconds.

Usage (from prototype/):
    uv run python scripts/export_incidents.py --out incidents.csv            # live, kind-repair
    uv run python scripts/export_incidents.py --from-file dump.json --out incidents.csv

``dump.json`` may be a list of RepairIncident objects, a Kubernetes list ``{"items": [...]}``
or a single object. Missing timestamps leave the duration cell empty.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

GROUP = "repair.example.dev"
VERSION = "v1alpha1"
PLURAL = "repairincidents"
DEFAULT_CONTEXT = "kind-repair"

COLUMNS = [
    "name",
    "ns",
    "fault",
    "class",
    "confidence",
    "tier",
    "source",
    "provider",
    "model",
    "latency_ms",
    "attempts",
    "outcome",
    "rejectedBy",
    "detected_to_collected_s",
    "collected_to_analyzed_s",
    "analyzed_to_prOpened_s",
    "prOpened_to_merged_s",
    "merged_to_synced_s",
    "synced_to_verified_s",
    "detected_to_prOpened_s",
]

# column -> (start timestamp key, end timestamp key); keys are status.timestamps.*
STAGES: dict[str, tuple[str, str]] = {
    "detected_to_collected_s": ("detected", "collected"),
    "collected_to_analyzed_s": ("collected", "analyzed"),
    "analyzed_to_prOpened_s": ("analyzed", "prOpened"),
    "prOpened_to_merged_s": ("prOpened", "merged"),  # human review time
    "merged_to_synced_s": ("merged", "synced"),
    "synced_to_verified_s": ("synced", "verified"),
    "detected_to_prOpened_s": ("detected", "prOpened"),  # total to PR
}


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _seconds(ts: dict, start: str, end: str) -> str:
    a, b = _parse_ts(ts.get(start)), _parse_ts(ts.get(end))
    if a is None or b is None:
        return ""
    return str(round((b - a).total_seconds()))


def _get(obj: dict, *path: str) -> Any:
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _fault(item: dict) -> str:
    labels = _get(item, "metadata", "labels") or {}
    annotations = _get(item, "metadata", "annotations") or {}
    value = (
        _get(item, "spec", "source", "fault")
        or _get(item, "status", "fault")
        or annotations.get(f"{GROUP}/fault")
        or labels.get(f"{GROUP}/fault")
    )
    return "" if value is None else str(value)


def row_for(item: dict) -> dict[str, str]:
    """One CSV row (keys = COLUMNS) for a RepairIncident object."""
    status = item.get("status") or {}
    ts = status.get("timestamps") or {}
    cls = status.get("classification") or {}
    proposal = status.get("proposal") or {}
    outcome = status.get("outcome") or {}

    def text(value: Any) -> str:
        return "" if value is None else str(value)

    row = {
        "name": text(_get(item, "metadata", "name")),
        "ns": text(_get(item, "metadata", "namespace")),
        "fault": _fault(item),
        "class": text(cls.get("class")),
        "confidence": text(cls.get("confidence")),
        "tier": text(proposal.get("tier")),
        "source": text(cls.get("source")),
        "provider": text(cls.get("provider")),
        "model": text(cls.get("model")),
        "latency_ms": text(cls.get("latencyMs")),
        "attempts": text(status.get("attempts")),
        "outcome": text(outcome.get("result")),
        "rejectedBy": text(proposal.get("rejectedBy")),
    }
    for column, (start, end) in STAGES.items():
        row[column] = _seconds(ts, start, end)
    return row


def load_from_file(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise TypeError(f"{path}: expected a list, a k8s list with 'items', or one object")
    return data


def load_live(context: str) -> list[dict]:
    from kubernetes import client, config  # imported lazily: not needed for --from-file

    config.load_kube_config(context=context)
    api = client.CustomObjectsApi()
    out = api.list_cluster_custom_object(GROUP, VERSION, PLURAL)
    return list(out.get("items", []))


def write_csv(items: list[dict], out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for item in items:
            writer.writerow(row_for(item))
    return len(items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-file", type=Path, help="JSON dump of RepairIncident objects")
    parser.add_argument("--out", type=Path, default=Path("incidents.csv"), help="CSV output path")
    parser.add_argument("--context", default=DEFAULT_CONTEXT, help="kube context for live mode")
    args = parser.parse_args(argv)

    items = load_from_file(args.from_file) if args.from_file else load_live(args.context)
    count = write_csv(items, args.out)
    print(f"wrote {count} incident(s) to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
