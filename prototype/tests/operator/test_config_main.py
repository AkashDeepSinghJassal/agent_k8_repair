from datetime import UTC, datetime
from pathlib import Path

from repair_operator import main
from repair_operator.config import Settings
from repair_operator.models import Edit, Phase, Proposal


def test_settings_defaults_and_env():
    s = Settings.from_env({})
    assert s.debounce_seconds == 60 and s.max_attempts == 3 and s.shadow_mode is False
    assert s.llm_model == "claude-sonnet-5-5" and s.state_dir == Path("~/.repair-loop").expanduser()
    s = Settings.from_env(
        {"REPAIR_SHADOW_MODE": "true", "REPAIR_DEBOUNCE_SECONDS": "5", "REPAIR_GITHUB_TOKEN": "secret"}
    )
    assert s.shadow_mode and s.debounce_seconds == 5
    pub = s.public()
    assert pub["github_token_set"] is True and "secret" not in str(pub)


def test_proposal_alias_and_phase():
    p = Proposal.model_validate(
        {
            "class": "OOM_KILLED",
            "root_cause": "x",
            "confidence": 0.9,
            "fixable_in_git": True,
            "file": "a.yaml",
            "edits": [{"find": "a", "replace": "b"}],
            "tier": 1,
            "source": "rules",
        }
    )
    assert p.cls == "OOM_KILLED" and p.edits == [Edit(find="a", replace="b")]
    assert Proposal(cls="X", root_cause="", confidence=1, fixable_in_git=False, tier=4, source="llm").cls == "X"
    assert Phase("Healed") is Phase.HEALED


def test_debounce_and_collect_step():
    t0 = "2026-11-03T10:00:00Z"
    now = datetime(2026, 11, 3, 10, 0, 20, tzinfo=UTC)
    assert main.debounce_remaining(t0, 60, now) == 40
    body = {"status": {"timestamps": {"detected": t0}}}
    patch: dict = {}
    assert main.collect_step(body, patch, 60, now) == 40 and patch == {}
    later = datetime(2026, 11, 3, 10, 1, 1, tzinfo=UTC)
    assert main.collect_step(body, patch, 60, later) == 0
    assert patch["status"]["phase"] == "Collected"


def test_phase_hooks():
    p: dict = {}
    main.analyze_hook({"status": {"attempts": 1}}, p)
    assert p["status"]["phase"] == "Analyzing" and p["status"]["attempts"] == 2
