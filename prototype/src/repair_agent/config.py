"""Provider selection: explicit arg > agent-config.json > REPAIR_AGENT_PROVIDER > groq if GROQ_API_KEY else claude-cli."""

from __future__ import annotations

import json
import os
from pathlib import Path

PROVIDERS = ("groq", "claude-cli")
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


def select_provider(explicit: str | None = None) -> str:
    cfg = read_config_file()
    for cand in (explicit, cfg.get("provider"), os.environ.get("REPAIR_AGENT_PROVIDER")):
        if cand in PROVIDERS:
            return cand
    return "groq" if os.environ.get("GROQ_API_KEY") else "claude-cli"


def other_provider(provider: str) -> str:
    return "claude-cli" if provider == "groq" else "groq"


def model_for(provider: str) -> str:
    """Model from agent-config.json only applies to the configured provider."""
    cfg = read_config_file()
    cfg_model = cfg.get("model") if cfg.get("provider") == provider else None
    if provider == "groq":
        return os.environ.get("REPAIR_AGENT_MODEL") or cfg_model or DEFAULT_GROQ_MODEL
    return os.environ.get("REPAIR_AGENT_CLAUDE_MODEL") or cfg_model or DEFAULT_CLAUDE_MODEL


def fallback_enabled() -> bool:
    return os.environ.get("REPAIR_AGENT_FALLBACK", "1") != "0"
