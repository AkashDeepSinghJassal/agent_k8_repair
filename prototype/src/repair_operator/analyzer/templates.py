"""Deterministic patch renderers.

Each ``render(bundle, params) -> list[Edit] | None`` works on the current file text in
``bundle["git"]["file"]`` and returns ``None`` when it cannot render safely (the analyzer then
escalates or falls back). Every returned ``find`` occurs exactly once in the file.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable

from ..models import Edit
from .rules import missing_ref

MI = 1024**2
GI = 1024**3
MEMORY_CAP = GI
MAX_FACTOR = 4

_QTY = re.compile(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|k|K|M|G|T)?$")
_UNITS = {None: 1, "Ki": 1024, "Mi": MI, "Gi": GI, "Ti": 1024**4, "k": 10**3, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}


def parse_quantity(s: str) -> int | None:
    m = _QTY.match(str(s).strip().strip("\"'"))
    if not m:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2)])


def format_quantity(n: int) -> str:
    if n % GI == 0:
        return f"{n // GI}Gi"
    if n % MI == 0:
        return f"{n // MI}Mi"
    return f"{-(-n // MI)}Mi"


def _file(bundle: dict) -> str | None:
    f = (bundle.get("git") or {}).get("file")
    return f if isinstance(f, str) and f else None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _unique_edit(text: str, idx: int, new_line: str) -> Edit | None:
    """Edit replacing line ``idx`` with ``new_line``, widened upwards until the find string is unique."""
    lines = text.split("\n")
    last = idx == len(lines) - 1
    for start in range(idx, -1, -1):
        seg = lines[start : idx + 1]
        find = "\n".join([seg[0].lstrip(" ")] + seg[1:]) + ("" if last else "\n")
        if text.count(find) == 1:
            new_seg = seg[:-1] + [new_line]
            repl = "\n".join([new_seg[0].lstrip(" ")] + new_seg[1:]) + ("" if last else "\n")
            return Edit(find=find, replace=repl)
    return None


def _container_blocks(lines: list[str], container: str | None) -> list[tuple[int, int]]:
    """(start, end) line ranges of ``- name: <container>`` list items."""
    blocks = []
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)-\s+name:\s*[\"']?([\w.\-]+)[\"']?\s*$", line)
        if not m or (container and m.group(2) != container):
            continue
        ind = len(m.group(1))
        end = i + 1
        while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > ind):
            end += 1
        blocks.append((i, end))
    return blocks


# ---------------------------------------------------------------- raise_memory_limit
def raise_memory_limit(bundle: dict, params: dict) -> list[Edit] | None:
    text = _file(bundle)
    if text is None:
        return None
    container = params.get("container") or (bundle.get("source") or {}).get("container")
    lines = text.split("\n")
    blocks = _container_blocks(lines, container) if container else _container_blocks(lines, None)
    for start, end in blocks:
        for i in range(start, end):
            if lines[i].strip() != "limits:":
                continue
            lim = _indent(lines[i])
            for j in range(i + 1, end):
                if lines[j].strip() and _indent(lines[j]) <= lim:
                    break
                m = re.match(r"^(\s*)memory:\s*[\"']?([\w.]+)[\"']?\s*(#.*)?$", lines[j])
                if not m:
                    continue
                old = parse_quantity(m.group(2))
                if old is None:
                    return None
                ceiling = min(old * MAX_FACTOR, MEMORY_CAP)
                want = parse_quantity(params["new_limit"]) if params.get("new_limit") else old * 2
                if want is None:
                    return None
                new = min(want, ceiling)
                if new <= old:
                    return None  # already at the cap; a bigger limit needs a human
                new_line = f"{m.group(1)}memory: {format_quantity(new)}"
                e = _unique_edit(text, j, new_line)
                return [e] if e else None
    return None


# ---------------------------------------------------------------- fix_image_tag
_IMG_LINE = re.compile(r"^[-+]\s*(?:-\s+)?image:\s*[\"']?([^\s\"']+)[\"']?\s*$")


def _split_image(ref: str) -> tuple[str, str | None]:
    name, _, tail = ref.rpartition(":")
    if not name or "/" in tail:
        return ref, None
    return name, tail


def fix_image_tag(bundle: dict, params: dict) -> list[Edit] | None:
    text = _file(bundle)
    diff = (bundle.get("git") or {}).get("lastCommitDiff")
    if text is None or not diff:
        return None
    removed, added = [], []
    for ln in diff.split("\n"):
        if ln.startswith(("---", "+++")):
            continue
        m = _IMG_LINE.match(ln)
        if m:
            (removed if ln.startswith("-") else added).append(m.group(1))
    spec_image = (bundle.get("spec") or {}).get("image")
    lines = text.split("\n")
    for bad in added:
        if spec_image and bad != spec_image:
            continue
        bad_repo, bad_tag = _split_image(bad)
        for good in removed:
            good_repo, good_tag = _split_image(good)
            if good_repo != bad_repo or not good_tag or good_tag == bad_tag:
                continue  # never change the registry/repository
            idxs = [i for i, ln in enumerate(lines) if re.match(rf"^\s*(-\s+)?image:\s*[\"']?{re.escape(bad)}[\"']?\s*$", ln)]
            if len(idxs) != 1:
                continue
            i = idxs[0]
            new_line = lines[i].replace(bad, good)
            e = _unique_edit(text, i, new_line)
            if e:
                return [e]
    return None


# ---------------------------------------------------------------- fix_config_ref
def _candidates(bundle: dict, params: dict, kind: str) -> list[str]:
    if params.get("candidates"):
        return list(params["candidates"])
    avail = bundle.get("available") or {}
    return list(avail.get("configmaps" if kind == "configmap" else "secrets") or [])


def fix_config_ref(bundle: dict, params: dict) -> list[Edit] | None:
    text = _file(bundle)
    if text is None:
        return None
    ref = missing_ref(bundle)
    kind = params.get("kind") or (ref[0] if ref else None)
    missing = params.get("missing") or (ref[1] if ref else None)
    if not kind or not missing:
        return None
    kind = kind.lower()

    replacement = params.get("replacement")
    diff = (bundle.get("git") or {}).get("lastCommitDiff") or ""
    if not replacement:
        removed, added = [], []
        for ln in diff.split("\n"):
            m = re.match(r"^([-+])\s*(?:-\s+)?(?:name|configMapName|secretName|configMapKeyRef|secretKeyRef):\s*[\"']?([\w.\-]+)[\"']?\s*$", ln)
            if m and not ln.startswith(("---", "+++")):
                (removed if m.group(1) == "-" else added).append(m.group(2))
        if missing in added:
            for good in removed:
                if good != missing:
                    replacement = good
                    break
    if not replacement:
        close = difflib.get_close_matches(missing, _candidates(bundle, params, kind), n=1, cutoff=0.6)
        replacement = close[0] if close else None
    if not replacement or replacement == missing:
        return None

    lines = text.split("\n")
    pat = re.compile(rf"^(\s*(?:-\s+)?(?:name|configMapName|secretName):\s*[\"']?){re.escape(missing)}([\"']?\s*)$")
    idxs = [i for i, ln in enumerate(lines) if pat.match(ln)]
    if not idxs or len(idxs) > 3:
        return None
    edits = []
    for i in idxs:
        new_line = pat.sub(lambda m: f"{m.group(1)}{replacement}{m.group(2)}", lines[i])
        e = _unique_edit(text, i, new_line)
        if e is None:
            return None
        edits.append(e)
    return edits


TEMPLATES: dict[str, Callable[[dict, dict], list[Edit] | None]] = {
    "raise_memory_limit": raise_memory_limit,
    "fix_image_tag": fix_image_tag,
    "fix_config_ref": fix_config_ref,
}


def render(template: str, bundle: dict, params: dict | None = None) -> list[Edit] | None:
    fn = TEMPLATES.get(template)
    if fn is None:
        return None
    try:
        edits = fn(bundle, params or {})
    except (KeyError, ValueError, TypeError):
        return None
    return edits or None
