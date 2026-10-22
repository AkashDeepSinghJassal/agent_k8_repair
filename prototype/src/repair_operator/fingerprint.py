"""Two fingerprints (plan v2 section 6, CONTRACTS section 5)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

_QUOTED = re.compile(r"""(["'`])(?:(?!\1).)+\1""")
_TS = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
)
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b")
_POD = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)*-[a-z0-9]{5,10}-[a-z0-9]{5}\b")
_HASH = re.compile(r"\b(?:sha256:)?[0-9a-fA-F]{12,}\b")
_NUM = re.compile(r"\b\d{4,}\b")
_WS = re.compile(r"\s+")


def normalise_line(line: str) -> str:
    """Replace the volatile parts of a log line with placeholders."""
    s = _QUOTED.sub("<NAME>", line)
    s = _TS.sub("<TS>", s)
    s = _IP.sub("<IP>", s)
    s = _POD.sub("<POD>", s)
    s = _HASH.sub("<HASH>", s)
    s = _NUM.sub("<N>", s)
    return _WS.sub(" ", s).strip()


def dedupe_fingerprint(namespace: str, workload: str, container_or_step: str, reason: str) -> str:
    """Incident identity: sha256 hex. Incident name is ``ri-`` + first 10 chars."""
    return hashlib.sha256(f"{namespace}|{workload}|{container_or_step}|{reason}".encode()).hexdigest()


def knowledge_signature(
    reason: str,
    exit_code: int | str | None,
    step_type: str,
    error_lines: Iterable[str],
    extra_codes: Iterable[str] = (),
) -> str:
    """12-hex failure signature; never includes namespace or workload names."""
    lines = sorted({n for n in (normalise_line(x) for x in error_lines) if n})
    codes = sorted(str(c) for c in extra_codes)
    payload = "|".join([reason, str(exit_code), step_type, *lines, *codes])
    return hashlib.sha256(payload.encode()).hexdigest()[:12]
