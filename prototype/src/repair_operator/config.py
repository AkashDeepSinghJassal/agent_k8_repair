"""Runtime settings, read from the environment (CONTRACTS section 3)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class Settings(BaseModel):
    github_token: str | None = Field(default=None, repr=False)
    demo_repo: str = "AkashDeepSinghJassal/repair-demo-app"
    demo_branch: str = "main"
    anthropic_api_key: str | None = Field(default=None, repr=False)
    llm_model: str = "claude-sonnet-5-5"
    analyzer_mode: str = "llm-first"  # "llm-first" | "rules-first"
    shadow_mode: bool = False
    debounce_seconds: int = 60
    verify_window_seconds: int = 120
    max_attempts: int = 3
    cooldown_seconds: int = 600
    state_dir: Path = Path("~/.repair-loop")
    promotion_threshold: int = 3

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        data: dict = {}
        mapping = {
            "github_token": "REPAIR_GITHUB_TOKEN",
            "demo_repo": "REPAIR_DEMO_REPO",
            "demo_branch": "REPAIR_DEMO_BRANCH",
            "anthropic_api_key": "ANTHROPIC_API_KEY",
            "llm_model": "REPAIR_LLM_MODEL",
            "analyzer_mode": "REPAIR_ANALYZER_MODE",
            "debounce_seconds": "REPAIR_DEBOUNCE_SECONDS",
            "verify_window_seconds": "REPAIR_VERIFY_WINDOW_SECONDS",
            "max_attempts": "REPAIR_MAX_ATTEMPTS",
            "cooldown_seconds": "REPAIR_COOLDOWN_SECONDS",
            "state_dir": "REPAIR_STATE_DIR",
            "promotion_threshold": "REPAIR_PROMOTION_THRESHOLD",
        }
        for field, var in mapping.items():
            if e.get(var):
                data[field] = e[var]
        data["shadow_mode"] = _bool(e.get("REPAIR_SHADOW_MODE"), False)
        s = cls(**data)
        s.state_dir = Path(s.state_dir).expanduser()
        return s

    def public(self) -> dict:
        """Settings safe to log (no secrets)."""
        d = self.model_dump(mode="json", exclude={"github_token", "anthropic_api_key"})
        d["github_token_set"] = bool(self.github_token)
        d["anthropic_api_key_set"] = bool(self.anthropic_api_key)
        return d


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
