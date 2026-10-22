"""Stage 1: deterministic rule table -> (class, confidence 1.0) or None."""

from __future__ import annotations

import re
from dataclasses import dataclass

TEMPLATE_FOR_CLASS = {
    "OOM_KILLED": "raise_memory_limit",
    "IMAGE_PULL": "fix_image_tag",
    "CONFIG_ERROR": "fix_config_ref",
}

_MISSING_REF = re.compile(r"(configmap|secret)\s+\"?([\w.\-]+)\"?\s+not found", re.IGNORECASE)
_IMAGE_MISSING = re.compile(r"not found|manifest unknown", re.IGNORECASE)


@dataclass
class RuleResult:
    cls: str
    confidence: float = 1.0
    hint: str = ""

    @property
    def template(self) -> str | None:
        return TEMPLATE_FOR_CLASS.get(self.cls)


def _texts(bundle: dict) -> list[str]:
    out = []
    for e in bundle.get("events") or []:
        out.append(f"{e.get('reason', '')} {e.get('message', '')}")
    f = bundle.get("failure") or {}
    out.append(f"{f.get('reason', '')} {f.get('message', '') or ''}")
    return out


def missing_ref(bundle: dict) -> tuple[str, str] | None:
    """(kind, name) of the first missing ConfigMap/Secret mentioned in events or failure."""
    for t in _texts(bundle):
        m = _MISSING_REF.search(t)
        if m:
            return m.group(1).lower(), m.group(2)
    return None


def classify(bundle: dict) -> RuleResult | None:
    failure = bundle.get("failure") or {}
    reason = str(failure.get("reason") or "")
    texts = _texts(bundle)
    joined = "\n".join(texts)

    if "FailedScheduling" in joined or reason == "Unschedulable":
        return RuleResult("UNSCHEDULABLE", hint="Pod cannot be scheduled; cause is cluster capacity")
    if reason == "OOMKilled":
        return RuleResult("OOM_KILLED", hint="resources.limits.memory in the source file")
    if "CreateContainerConfigError" in joined:
        if missing_ref(bundle):
            return RuleResult("CONFIG_ERROR", hint="the referenced ConfigMap or Secret name")
        return None
    if "ErrImagePull" in joined or "ImagePullBackOff" in joined:
        if any(_IMAGE_MISSING.search(t) for t in texts):
            return RuleResult("IMAGE_PULL", hint="the image: tag")
        return None  # auth or registry trouble: let the LLM decide
    if bundle.get("workflow"):
        return RuleResult("STAGE_FAILURE", hint="the WorkflowTemplate step")
    if "CrashLoopBackOff" in joined or reason == "Error":
        return RuleResult("CRASH_LOOP", hint="none; read the logs")
    return None
