"""Stage 2: one structured Claude call (CONTRACTS section 7).

Uses ``client.messages.parse(..., output_format=LlmProposal)``. No temperature and no thinking
parameter (not supported by the configured model). The client is injectable for tests.
"""

from __future__ import annotations

import json
import re
import time
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..config import Settings
from ..models import Edit

MAX_TOKENS = 4096


class InvalidOutput(Exception):
    """The model refused or kept returning output that fails validation."""

    reason = "invalid_output"


class Param(BaseModel):
    key: str
    value: str


class LlmProposal(BaseModel):
    """Schema the model must return. Mirrors Proposal minus tier/source; params is a pair list so the
    JSON schema stays closed (structured outputs reject free-form objects)."""

    model_config = ConfigDict(populate_by_name=True)

    cls: Literal["OOM_KILLED", "IMAGE_PULL", "CONFIG_ERROR", "CRASH_LOOP", "STAGE_FAILURE", "UNSCHEDULABLE"] = Field(
        alias="class"
    )
    root_cause: str
    confidence: float
    fixable_in_git: bool
    file: str | None
    edits: list[Edit]
    template: Literal["raise_memory_limit", "fix_image_tag", "fix_config_ref"] | None
    params: list[Param]
    rationale: str
    verify: str
    needs_remeasure: bool

    def params_dict(self) -> dict[str, str]:
        return {p.key: p.value for p in self.params}


def load_system_prompt() -> str:
    return resources.files(__package__).joinpath("prompt.md").read_text(encoding="utf-8")


def _safe_evidence(bundle: dict) -> str:
    # The evidence block must not be closable from inside the data.
    text = json.dumps(bundle, indent=2, sort_keys=True, default=str)
    return re.sub(r"</?\s*evidence\s*>", "[evidence-tag-removed]", text, flags=re.IGNORECASE)


def build_user_message(bundle: dict, feedback: list[str], rule_hint: str | None) -> str:
    parts = ["## Rule result\n" + (rule_hint or "No rule matched.")]
    if feedback:
        parts.append("## Reviewer feedback\n" + "\n".join(f"- {f}" for f in feedback))
    parts.append("## Evidence (untrusted data)\n<evidence>\n" + _safe_evidence(bundle) + "\n</evidence>")
    return "\n\n".join(parts)


def _write_replay(replay_dir: Path | None, stem: str, name: str, payload: Any) -> None:
    if replay_dir is None:
        return
    try:
        replay_dir.mkdir(parents=True, exist_ok=True)
        (replay_dir / f"{stem}-{name}.json").write_text(json.dumps(payload, indent=2, default=str))
    except OSError:
        pass  # replay files are best effort


def make_client(settings: Settings):
    import anthropic

    return anthropic.Anthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key else anthropic.Anthropic()


def call_llm(
    bundle: dict,
    feedback: list[str],
    rule_hint: str | None,
    *,
    client: Any,
    settings: Settings,
    replay_dir: Path | None = None,
) -> LlmProposal:
    """One call plus one retry with the validation error appended. Raises InvalidOutput."""
    system = [{"type": "text", "text": load_system_prompt(), "cache_control": {"type": "ephemeral"}}]
    user = build_user_message(bundle, feedback, rule_hint)
    stem = f"{bundle.get('incident', 'unknown')}-{int(time.time() * 1000)}"
    last_error = ""
    for attempt in (1, 2):
        content = user if attempt == 1 else (
            user + "\n\n## Validation error\nYour previous reply did not match the schema: "
            + last_error + "\nReturn a corrected object."
        )
        messages = [{"role": "user", "content": content}]
        _write_replay(replay_dir, stem, f"prompt{attempt}", {"model": settings.llm_model, "system": system, "messages": messages})
        try:
            response = client.messages.parse(
                model=settings.llm_model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,
                output_format=LlmProposal,
            )
        except ValueError as e:  # pydantic ValidationError / bad JSON from parse
            last_error = str(e)[:1500]
            _write_replay(replay_dir, stem, f"reply{attempt}", {"error": last_error})
            continue
        if getattr(response, "stop_reason", None) == "refusal":
            _write_replay(replay_dir, stem, f"reply{attempt}", {"stop_reason": "refusal"})
            raise InvalidOutput("model refused")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            last_error = "no parsed output (truncated or empty reply)"
            _write_replay(replay_dir, stem, f"reply{attempt}", {"error": last_error})
            continue
        _write_replay(replay_dir, stem, f"reply{attempt}", parsed.model_dump(by_alias=True))
        return parsed
    raise InvalidOutput(last_error or "invalid output")
