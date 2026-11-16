"""Provider selection: explicit arg > agent-config.json > REPAIR_AGENT_PROVIDER > groq if GROQ_API_KEY else claude-cli."""

from __future__ import annotations

import json
import os
from pathlib import Path

SCADS_LEVELS = {
    "scads-high": ("REPAIR_SCADS_MODEL_HIGH", "mistralai/Mistral-Large-4"),
    "scads-medium": ("REPAIR_SCADS_MODEL_MEDIUM", "meta-llama/Llama-3.3-70B-Instruct"),
    "scads-low": ("REPAIR_SCADS_MODEL_LOW", "google/gemma-4-26B-A4B-it"),
}
PROVIDERS = ("groq", "claude-cli", *SCADS_LEVELS)
SCADS_BASE_URL = "https://llm.scads.ai/v1"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_CLAUDE_MODEL = "sonnet"


def state_dir() -> Path:
    return Path(os.environ.get("REPAIR_STATE_DIR") or Path.home() / ".repair-loop")


def read_config_file() -> dict:
    try:
        data = json.loads((state_dir() / "agent-config.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_scads(provider: str) -> bool:
    return provider in SCADS_LEVELS or provider.startswith("scads:")


def valid_provider(cand: object) -> bool:
    return isinstance(cand, str) and (cand in PROVIDERS or (cand.startswith("scads:") and len(cand) > 6))


def scads_api_key() -> str | None:
    """SCADS_API_KEY from the environment, else from ``~/.repair-loop/scads.env``. Never logged."""
    if key := os.environ.get("SCADS_API_KEY"):
        return key.strip()
    try:
        lines = (state_dir() / "scads.env").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        name, _, value = line.strip().partition("=")
        if name.replace("export ", "").strip() == "SCADS_API_KEY" and value.strip():
            return value.strip().strip("'\"")
    return None


def select_provider(explicit: str | None = None) -> str:
    cfg = read_config_file()
    for cand in (explicit, cfg.get("provider"), os.environ.get("REPAIR_AGENT_PROVIDER")):
        if valid_provider(cand):
            return cand  # type: ignore[return-value]
    return "groq" if os.environ.get("GROQ_API_KEY") else "claude-cli"


def other_provider(provider: str) -> str:
    return "groq" if provider == "claude-cli" else "claude-cli"


def model_for(provider: str) -> str:
    """Model from agent-config.json only applies to the configured provider."""
    cfg = read_config_file()
    cfg_model = cfg.get("model") if cfg.get("provider") == provider else None
    if provider.startswith("scads:"):
        return provider[6:]
    if provider in SCADS_LEVELS:
        env, default = SCADS_LEVELS[provider]
        return os.environ.get(env) or default
    if provider == "groq":
        return os.environ.get("REPAIR_AGENT_MODEL") or cfg_model or DEFAULT_GROQ_MODEL
    return os.environ.get("REPAIR_AGENT_CLAUDE_MODEL") or cfg_model or DEFAULT_CLAUDE_MODEL


def fallback_enabled() -> bool:
    return os.environ.get("REPAIR_AGENT_FALLBACK", "1") != "0"
