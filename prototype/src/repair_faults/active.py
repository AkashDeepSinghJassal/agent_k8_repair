"""Record which fault is active per namespace so the operator can tag `detected` trace events.

Shared by the dashboard and the faults CLI. Writes ``<state_dir>/active-faults.json``; best effort.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path


def record_active_fault(namespace: str, fault_id: str | None) -> None:
    """Set (``fault_id``) or clear (None) the active fault for ``namespace``. Never raises."""
    try:
        from repair_operator.config import get_settings

        path = Path(get_settings().state_dir).expanduser() / "active-faults.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        if fault_id:
            data[namespace] = {"fault": fault_id, "ts": time.time()}
        else:
            data.pop(namespace, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except Exception:
        logging.getLogger(__name__).debug("active-faults write failed", exc_info=True)
