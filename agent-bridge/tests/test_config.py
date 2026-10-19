from pathlib import Path

import pytest

from agent_bridge.config import Settings

TOKEN = "t" * 32


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ("AGENT_BRIDGE_CLAUDE_CONFIG_DIR", "AGENT_BRIDGE_TRUST_ROOT", "AGENT_BRIDGE_CLAUDE_ARGS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("AGENT_BRIDGE_ALLOWED_ROOTS", str(tmp_path))
    return monkeypatch


def test_defaults_use_claudes_own_config_and_trust_nothing(env: pytest.MonkeyPatch) -> None:
    settings = Settings.from_env()
    assert settings.claude_config_dir is None
    assert settings.trust_root is None
    assert settings.claude_env() == {}


def test_reads_a_separate_config_and_trust_root(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env.setenv("AGENT_BRIDGE_CLAUDE_CONFIG_DIR", str(tmp_path / ".claude-agents"))
    env.setenv("AGENT_BRIDGE_TRUST_ROOT", str(tmp_path))
    settings = Settings.from_env()
    assert settings.claude_env() == {"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-agents")}
    assert settings.trust_root == tmp_path


def test_refuses_a_trust_root_without_a_separate_config(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env.setenv("AGENT_BRIDGE_TRUST_ROOT", str(tmp_path))
    with pytest.raises(RuntimeError, match="AGENT_BRIDGE_CLAUDE_CONFIG_DIR"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [("AGENT_BRIDGE_TOKEN", "short", "at least 32"), ("AGENT_BRIDGE_ALLOWED_ROOTS", "", "at least one")],
)
def test_refuses_missing_required_settings(
    env: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    env.setenv(name, value)
    with pytest.raises(RuntimeError, match=message):
        Settings.from_env()
