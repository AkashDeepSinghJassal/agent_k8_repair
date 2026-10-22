"""Stage 2: one structured LLM call (CONTRACTS section 7) behind a small ``LlmClient`` protocol.

``AnthropicClient``: ``messages.parse(..., output_format=LlmProposal)``, no temperature/thinking parameter.
``GroqClient``: chat.completions with a strict JSON-schema response format, pydantic validation.
``call_llm`` owns replay files and the one retry with the validation error appended. Clients are injectable.
"""

from __future__ import annotations

import json
import re
import time
from importlib import resources
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from ..config import Settings
from ..models import Edit

MAX_TOKENS = 4096


class InvalidOutput(Exception):
    """The model refused or kept returning output that fails validation."""

    reason = "invalid_output"


class LlmUnavailable(Exception):
    """Auth/permission/billing failure: retrying cannot help, escalate immediately."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SchemaError(Exception):
    """Reply was received but is not a valid LlmProposal (retryable once)."""


class LlmClient(Protocol):
    provider: str
    model: str

    def propose(self, system: str, user: str) -> LlmProposal:
        """Return a validated proposal; raise SchemaError (retryable), InvalidOutput (refusal) or LlmUnavailable."""


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

    _meta: dict = PrivateAttr(default_factory=dict)  # provider/model/latencyMs/usage; not part of the schema

    @property
    def meta(self) -> dict:
        return self._meta

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


def _strict_schema() -> dict:
    """LlmProposal JSON schema with additionalProperties=false on every object (strict mode)."""
    schema = LlmProposal.model_json_schema(by_alias=True)

    def close(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for v in node.values():
                close(v)
        elif isinstance(node, list):
            for v in node:
                close(v)

    close(schema)
    schema.pop("title", None)
    return schema


def _unavailable_reason(exc: Exception) -> str | None:
    """Reason string when the exception means 'retrying cannot help' (auth, permission, billing, quota)."""
    msg = str(getattr(exc, "message", None) or exc)
    for mod in ("anthropic", "groq"):
        try:
            sdk = __import__(mod)
        except ImportError:  # pragma: no cover
            continue
        if isinstance(exc, (sdk.AuthenticationError, sdk.PermissionDeniedError)):
            return msg[:200]
        if isinstance(exc, sdk.BadRequestError) and re.search(r"credit|billing|quota", msg, re.IGNORECASE):
            return msg[:200]
    return None


class AnthropicClient:
    provider = "anthropic"

    def __init__(self, raw: Any, model: str):
        self.raw, self.model = raw, model

    def propose(self, system: str, user: str) -> LlmProposal:
        blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        try:
            response = self.raw.messages.parse(
                model=self.model, max_tokens=MAX_TOKENS, system=blocks,
                messages=[{"role": "user", "content": user}], output_format=LlmProposal,
            )
        except Exception as e:
            if (reason := _unavailable_reason(e)) is not None:
                raise LlmUnavailable(reason) from e
            if isinstance(e, ValueError):  # pydantic ValidationError / bad JSON from parse
                raise SchemaError(str(e)[:1500]) from e
            raise
        if getattr(response, "stop_reason", None) == "refusal":
            raise InvalidOutput("model refused")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise SchemaError("no parsed output (truncated or empty reply)")
        usage = getattr(response, "usage", None)
        if usage is not None:
            parsed.meta["usage"] = {"input": getattr(usage, "input_tokens", None), "output": getattr(usage, "output_tokens", None)}
        return parsed


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
MAX_RATE_LIMIT_RETRIES = 2
MAX_BACKOFF_SECONDS = 30.0


def _extract_json(text: str) -> str:
    text = _THINK.sub("", text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return text


class GroqClient:
    provider = "groq"
    max_tokens = 8192  # gpt-oss spends part of the budget on reasoning

    def __init__(self, raw: Any, model: str, reasoning_effort: str = "medium", sleep=time.sleep):
        self.raw, self.model, self.reasoning_effort, self._sleep = raw, model, reasoning_effort, sleep
        self._schema = _strict_schema()

    def _create(self, system: str, user: str):
        import groq

        kw: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_completion_tokens": self.max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {"name": "repair_proposal", "schema": self._schema, "strict": True}},
        }
        if self.model.startswith("openai/gpt-oss") and self.reasoning_effort:
            kw["reasoning_effort"] = self.reasoning_effort
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                return self.raw.chat.completions.create(**kw)
            except groq.RateLimitError as e:
                msg = str(getattr(e, "message", None) or e)
                if re.search(r"per day|TPD|RPD", msg):
                    raise LlmUnavailable(("daily quota: " + msg)[:200]) from e
                if attempt == MAX_RATE_LIMIT_RETRIES:
                    raise InvalidOutput("rate_limited") from e
                try:
                    wait = float(e.response.headers.get("retry-after", 2))
                except (AttributeError, TypeError, ValueError):
                    wait = 2.0
                self._sleep(min(max(wait, 0.5), MAX_BACKOFF_SECONDS))
            except Exception as e:
                if (reason := _unavailable_reason(e)) is not None:
                    raise LlmUnavailable(reason) from e
                if isinstance(e, groq.BadRequestError):  # e.g. json_validate_failed: model produced bad JSON
                    raise SchemaError(str(getattr(e, "message", None) or e)[:1500]) from e
                raise

    def propose(self, system: str, user: str) -> LlmProposal:
        response = self._create(system, user)
        choice = response.choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            raise SchemaError("reply truncated (max tokens reached)")
        text = _extract_json(choice.message.content or "")
        try:
            parsed = LlmProposal.model_validate_json(text)
        except ValidationError as e:
            raise SchemaError(str(e)[:1500]) from e
        usage = getattr(response, "usage", None)
        if usage is not None:
            parsed.meta["usage"] = {"input": getattr(usage, "prompt_tokens", None), "output": getattr(usage, "completion_tokens", None)}
        return parsed


def make_client(settings: Settings) -> LlmClient:
    if settings.llm_provider == "anthropic":
        import anthropic

        raw = anthropic.Anthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key else anthropic.Anthropic()
        return AnthropicClient(raw, settings.llm_model)
    import groq

    raw = groq.Groq(api_key=settings.groq_api_key) if settings.groq_api_key else groq.Groq()
    return GroqClient(raw, settings.llm_model, settings.llm_reasoning_effort)


def _as_llm_client(client: Any, settings: Settings) -> LlmClient:
    """Accept an LlmClient, or a raw SDK client (tests) which is wrapped for the configured provider."""
    if hasattr(client, "propose"):
        return client
    if settings.llm_provider == "anthropic":
        return AnthropicClient(client, settings.llm_model)
    return GroqClient(client, settings.llm_model, settings.llm_reasoning_effort)


def call_llm(
    bundle: dict,
    feedback: list[str],
    rule_hint: str | None,
    *,
    client: Any,
    settings: Settings,
    replay_dir: Path | None = None,
) -> LlmProposal:
    """One call plus one retry with the validation error appended. Raises InvalidOutput / LlmUnavailable."""
    llm = _as_llm_client(client, settings)
    system = load_system_prompt()
    user = build_user_message(bundle, feedback, rule_hint)
    stem = f"{bundle.get('incident', 'unknown')}-{int(time.time() * 1000)}"
    who = {"provider": llm.provider, "model": llm.model}
    last_error = ""
    for attempt in (1, 2):
        content = user if attempt == 1 else (
            user + "\n\n## Validation error\nYour previous reply did not match the schema: "
            + last_error + "\nReturn a corrected object."
        )
        _write_replay(replay_dir, stem, f"prompt{attempt}", {**who, "system": system, "messages": [{"role": "user", "content": content}]})
        started = time.monotonic()
        try:
            parsed = llm.propose(system, content)
        except LlmUnavailable as e:
            _write_replay(replay_dir, stem, f"reply{attempt}", {**who, "error": e.reason})
            raise
        except InvalidOutput as e:
            _write_replay(replay_dir, stem, f"reply{attempt}", {**who, "error": str(e)})
            raise
        except SchemaError as e:
            last_error = str(e)
            _write_replay(replay_dir, stem, f"reply{attempt}", {**who, "error": last_error})
            continue
        parsed.meta.update(who, latencyMs=round((time.monotonic() - started) * 1000))
        _write_replay(replay_dir, stem, f"reply{attempt}", {**who, "latencyMs": parsed.meta["latencyMs"], "reply": parsed.model_dump(by_alias=True)})
        return parsed
    raise InvalidOutput(last_error or "invalid output")
