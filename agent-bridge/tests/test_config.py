from pathlib import Path

import pytest

from agent_bridge.config import Settings

TOKEN = "t" * 32


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    # from_env reads .env from the working directory; start each test without one.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("AGENT_BRIDGE_ALLOWED_ROOTS", str(tmp_path))
    return monkeypatch


def test_defaults_use_claudes_own_config_and_trust_nothing(env: pytest.MonkeyPatch) -> None:
    settings = Settings.from_env()
    assert settings.claude_config_dir is None
    assert settings.trust_root is None
    assert settings.claude_env() == {}
    assert settings.claude_args == ("--permission-mode", "auto")


def test_reads_a_separate_config_and_trust_root(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env.setenv("AGENT_BRIDGE_CLAUDE_CONFIG_DIR", str(tmp_path / ".claude-agents"))
    env.setenv("AGENT_BRIDGE_TRUST_ROOT", str(tmp_path))
    settings = Settings.from_env()
    assert settings.claude_env() == {"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-agents")}
    assert settings.trust_root == tmp_path


def test_empty_optional_paths_are_unset(env: pytest.MonkeyPatch) -> None:
    env.setenv("AGENT_BRIDGE_CLAUDE_CONFIG_DIR", "")
    env.setenv("AGENT_BRIDGE_TRUST_ROOT", "")
    settings = Settings.from_env()
    assert (settings.claude_config_dir, settings.trust_root) == (None, None)


def test_refuses_a_trust_root_without_a_separate_config(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env.setenv("AGENT_BRIDGE_TRUST_ROOT", str(tmp_path))
    with pytest.raises(RuntimeError, match="AGENT_BRIDGE_CLAUDE_CONFIG_DIR"):
        Settings.from_env()


@pytest.mark.parametrize("config_dir", ["", ".claude", ".claude/nested"])
def test_refuses_the_default_config_as_the_separate_one(
    env: pytest.MonkeyPatch, tmp_path: Path, config_dir: str
) -> None:
    # The allowed root must not be the home folder itself, so keep them apart.
    home = tmp_path / "home"
    env.setenv("HOME", str(home))
    env.setenv("AGENT_BRIDGE_ALLOWED_ROOTS", str(tmp_path / "projects"))
    (home / config_dir).mkdir(parents=True, exist_ok=True)
    env.setenv("AGENT_BRIDGE_CLAUDE_CONFIG_DIR", str(home / config_dir))
    with pytest.raises(RuntimeError, match="AGENT_BRIDGE_CLAUDE_CONFIG_DIR.*default Claude config"):
        Settings.from_env()


def test_splits_roots_and_shell_quoted_args(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    env.setenv("AGENT_BRIDGE_ALLOWED_ROOTS", f"{tmp_path / 'a'}::{tmp_path / 'b'}")
    env.setenv("AGENT_BRIDGE_CLAUDE_ARGS", "--model 'opus 5' --verbose")
    settings = Settings.from_env()
    assert settings.allowed_roots == ((tmp_path / "a").resolve(), (tmp_path / "b").resolve())
    assert settings.claude_args == ("--model", "opus 5", "--verbose")


def test_empty_claude_args_pass_no_arguments(env: pytest.MonkeyPatch) -> None:
    env.setenv("AGENT_BRIDGE_CLAUDE_ARGS", "")
    assert Settings.from_env().claude_args == ()


def test_every_limit_is_settable(env: pytest.MonkeyPatch) -> None:
    env.setenv("AGENT_BRIDGE_MAX_AGENTS", "3")
    env.setenv("AGENT_BRIDGE_MAX_OUTPUT_CHARS", "2000")
    env.setenv("AGENT_BRIDGE_PROMPT_TIMEOUT_MS", "60000")
    settings = Settings.from_env()
    assert (settings.max_agents, settings.max_output_chars, settings.prompt_timeout_ms) == (
        3,
        2000,
        60000,
    )


def test_reads_a_dotenv_file_under_the_environment(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    Path(".env").write_text("AGENT_BRIDGE_MAX_AGENTS=4\nAGENT_BRIDGE_HERDR_BIN=/opt/herdr\nOTHER=x\n")
    env.setenv("AGENT_BRIDGE_HERDR_BIN", "/usr/bin/herdr")
    settings = Settings.from_env()
    assert (settings.max_agents, settings.herdr_bin) == (4, "/usr/bin/herdr")


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("AGENT_BRIDGE_TOKEN", "short", "AGENT_BRIDGE_TOKEN: .*at least 32"),
        ("AGENT_BRIDGE_ALLOWED_ROOTS", "", "AGENT_BRIDGE_ALLOWED_ROOTS: must list at least one"),
        ("AGENT_BRIDGE_ALLOWED_ROOTS", ":", "AGENT_BRIDGE_ALLOWED_ROOTS: must list at least one"),
        ("AGENT_BRIDGE_MAX_AGENTS", "ten", "AGENT_BRIDGE_MAX_AGENTS: .*integer"),
    ],
)
def test_refuses_bad_settings_naming_the_variable(
    env: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    env.setenv(name, value)
    with pytest.raises(RuntimeError, match=message):
        Settings.from_env()


@pytest.mark.parametrize("name", ["AGENT_BRIDGE_TOKEN", "AGENT_BRIDGE_ALLOWED_ROOTS"])
def test_required_settings_must_be_set(env: pytest.MonkeyPatch, name: str) -> None:
    env.delenv(name)
    with pytest.raises(RuntimeError, match=f"{name}: must be set"):
        Settings.from_env()


@pytest.mark.parametrize("variable", ["AGENT_BRIDGE_ALLOWED_ROOTS", "AGENT_BRIDGE_TRUST_ROOT"])
@pytest.mark.parametrize("root", ["/", "{home}", "{home}/..", "projects", "./projects"])
def test_refuses_roots_that_are_too_broad_or_relative(
    env: pytest.MonkeyPatch, tmp_path: Path, variable: str, root: str
) -> None:
    # "/" or the home folder would let agents start (or be trusted) anywhere; a relative
    # path would resolve against launchd's working folder, which is "/".
    home = tmp_path / "home" / "me"
    home.mkdir(parents=True)
    env.setenv("HOME", str(home))
    env.setenv("AGENT_BRIDGE_CLAUDE_CONFIG_DIR", str(home / ".claude-agents"))
    env.setenv(variable, root.format(home=home))
    with pytest.raises(RuntimeError, match=variable):
        Settings.from_env()


def test_accepts_a_project_folder_under_home(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home" / "me"
    (home / "projects").mkdir(parents=True)
    env.setenv("HOME", str(home))
    env.setenv("AGENT_BRIDGE_ALLOWED_ROOTS", "~/projects")
    env.setenv("AGENT_BRIDGE_CLAUDE_CONFIG_DIR", str(home / ".claude-agents"))
    env.setenv("AGENT_BRIDGE_TRUST_ROOT", str(home / "projects"))
    settings = Settings.from_env()
    assert settings.allowed_roots == ((home / "projects").resolve(),)
