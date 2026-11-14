import json
import os
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_bridge.config import Settings, claude_env

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
    assert claude_env(settings.claude_config_dir) == {}
    assert settings.claude_args == ("--permission-mode", "auto")


def test_reads_a_separate_config_and_trust_root(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env.setenv("AGENT_BRIDGE_CLAUDE_CONFIG_DIR", str(tmp_path / ".claude-agents"))
    env.setenv("AGENT_BRIDGE_TRUST_ROOT", str(tmp_path))
    settings = Settings.from_env()
    assert claude_env(settings.claude_config_dir) == {"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-agents")}
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
    env: pytest.MonkeyPatch, tmp_path: Path, config_dir: str, set_home: Callable[[Path], None]
) -> None:
    # The allowed root must not be the home folder itself, so keep them apart.
    home = tmp_path / "home"
    set_home(home)
    env.setenv("AGENT_BRIDGE_ALLOWED_ROOTS", str(tmp_path / "projects"))
    (home / config_dir).mkdir(parents=True, exist_ok=True)
    env.setenv("AGENT_BRIDGE_CLAUDE_CONFIG_DIR", str(home / config_dir))
    with pytest.raises(RuntimeError, match="AGENT_BRIDGE_CLAUDE_CONFIG_DIR.*default Claude config"):
        Settings.from_env()


def test_splits_roots_and_shell_quoted_args(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    env.setenv("AGENT_BRIDGE_ALLOWED_ROOTS", os.pathsep.join([str(tmp_path / "a"), "", str(tmp_path / "b")]))
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
        ("AGENT_BRIDGE_ALLOWED_ROOTS", os.pathsep, "AGENT_BRIDGE_ALLOWED_ROOTS: must list at least one"),
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
    env: pytest.MonkeyPatch,
    tmp_path: Path,
    variable: str,
    root: str,
    set_home: Callable[[Path], None],
) -> None:
    # "/" or the home folder would let agents start (or be trusted) anywhere; a relative
    # path would resolve against launchd's working folder, which is "/".
    home = tmp_path / "home" / "me"
    home.mkdir(parents=True)
    set_home(home)
    env.setenv("AGENT_BRIDGE_CLAUDE_CONFIG_DIR", str(home / ".claude-agents"))
    env.setenv(variable, root.format(home=home))
    with pytest.raises(RuntimeError, match=variable):
        Settings.from_env()


def test_accepts_a_project_folder_under_home(
    env: pytest.MonkeyPatch, tmp_path: Path, set_home: Callable[[Path], None]
) -> None:
    home = tmp_path / "home" / "me"
    (home / "projects").mkdir(parents=True)
    set_home(home)
    env.setenv("AGENT_BRIDGE_ALLOWED_ROOTS", "~/projects")
    env.setenv("AGENT_BRIDGE_CLAUDE_CONFIG_DIR", str(home / ".claude-agents"))
    env.setenv("AGENT_BRIDGE_TRUST_ROOT", str(home / "projects"))
    settings = Settings.from_env()
    assert settings.allowed_roots == ((home / "projects").resolve(),)


def test_reads_accounts_as_json(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "work" / "app").mkdir(parents=True)
    accounts = {
        "work": {"config_dir": None, "folders": [str(tmp_path / "work")]},
        "personal": {"config_dir": str(tmp_path / ".claude-personal"), "folders": [str(tmp_path)]},
    }
    env.setenv("AGENT_BRIDGE_ACCOUNTS", json.dumps(accounts))
    settings = Settings.from_env()
    assert settings.accounts["work"].config_dir is None
    assert settings.accounts["personal"].config_dir == tmp_path / ".claude-personal"
    # The closest folder wins: work's is inside personal's.
    assert settings.account_for((tmp_path / "work" / "app").resolve()) == "work"
    assert settings.account_for(tmp_path.resolve()) == "personal"
    assert settings.account_for(Path("/elsewhere")) is None


@pytest.mark.parametrize(
    ("accounts", "problem"),
    [
        ('{"Work": {}}', "lower-case"),
        ('{"work": {"config_dir": "relative/dir"}}', "absolute"),
        ('{"work": {"folders": ["{drive}"]}}', "too broad"),
        ('{"work": {"folders": ["{root}"]}, "home": {"folders": ["{root}"]}}', "in both"),
        ('{"work": {"config": "/x"}}', r"AGENT_BRIDGE_ACCOUNTS \(work.config\): Extra inputs"),
    ],
)
def test_refuses_bad_accounts(env: pytest.MonkeyPatch, tmp_path: Path, accounts: str, problem: str) -> None:
    # "/" on macOS and Linux, "C:/" on Windows.
    drive = Path(tmp_path.anchor).as_posix()
    env.setenv("AGENT_BRIDGE_ACCOUNTS", accounts.replace("{root}", tmp_path.as_posix()).replace("{drive}", drive))
    with pytest.raises(RuntimeError, match=problem):
        Settings.from_env()


def test_reads_the_env_file_over_a_legacy_dotenv(env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    env.delenv("AGENT_BRIDGE_TOKEN")
    Path(os.environ["AGENT_BRIDGE_ENV_FILE"]).write_text(
        f"AGENT_BRIDGE_TOKEN={TOKEN}\nAGENT_BRIDGE_HOST=192.168.1.20\nAGENT_BRIDGE_NAME=studio\n"
    )
    Path(".env").write_text("AGENT_BRIDGE_NAME=old\nAGENT_BRIDGE_PORT=9000\n")

    settings = Settings.from_env()

    assert (settings.token, settings.host, settings.name, settings.port) == (TOKEN, "192.168.1.20", "studio", 9000)
    assert settings.url == "http://192.168.1.20:9000"
    assert "move them to" in caplog.text


def test_reads_only_the_env_file_without_a_legacy_dotenv(
    env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A checkout's .env may be mcp-gateway's: no warning for that.
    Path(".env").write_text("MCP_GATEWAY_PUBLIC_HOST=mcp.example.com\n")
    Path(os.environ["AGENT_BRIDGE_ENV_FILE"]).write_text("AGENT_BRIDGE_NAME=studio\n")

    assert Settings.from_env().name == "studio"
    assert "move them to" not in caplog.text


def test_reads_a_legacy_dotenv_written_with_export_or_lowercase(
    env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    env.delenv("AGENT_BRIDGE_TOKEN")
    Path(".env").write_text(f"export AGENT_BRIDGE_TOKEN={TOKEN}\nagent_bridge_port=9000\n")

    settings = Settings.from_env()

    assert (settings.token, settings.port) == (TOKEN, 9000)
    assert "move them to" in caplog.text


def test_names_the_env_file_in_errors(env: pytest.MonkeyPatch) -> None:
    env.delenv("AGENT_BRIDGE_TOKEN")
    with pytest.raises(RuntimeError, match=f"env file: {re.escape(os.environ['AGENT_BRIDGE_ENV_FILE'])}"):
        Settings.from_env()


def test_the_service_needs_host_name_and_herdr(env: pytest.MonkeyPatch) -> None:
    assert Settings.from_env().missing_for_service() == [
        "AGENT_BRIDGE_HOST", "AGENT_BRIDGE_NAME", "AGENT_BRIDGE_HERDR_BIN",
    ]
    env.setenv("AGENT_BRIDGE_HOST", "fd00::5")
    env.setenv("AGENT_BRIDGE_NAME", "mac")
    env.setenv("AGENT_BRIDGE_HERDR_BIN", "/opt/herdr")
    settings = Settings.from_env()
    assert settings.missing_for_service() == []
    assert settings.url == "http://[fd00::5]:8765"


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("AGENT_BRIDGE_HOST", "mac.local", "IP address"),
        ("AGENT_BRIDGE_HOST", "0.0.0.0", "every interface"),
        ("AGENT_BRIDGE_NAME", "Studio Mac", "lowercase letter"),
        ("AGENT_BRIDGE_NAME", "a" * 33, "lowercase letter"),
        ("AGENT_BRIDGE_PORT", "70000", "less than or equal"),
        ("AGENT_BRIDGE_TOKEN", "t" * 31 + " ", "may only have"),
    ],
)
def test_refuses_a_bad_host_name_port_or_token(env: pytest.MonkeyPatch, name: str, value: str, message: str) -> None:
    env.setenv(name, value)
    with pytest.raises(RuntimeError, match=f"{name}: .*{message}"):
        Settings.from_env()


@pytest.mark.skipif(os.name != "nt", reason="only Windows has \\\\ paths")
@pytest.mark.parametrize("prefix", ["\\\\?\\", "\\\\localhost\\C$\\"])
def test_refuses_roots_spelled_as_unc_or_long_paths(
    env: pytest.MonkeyPatch, tmp_path: Path, set_home: Callable[[Path], None], prefix: str
) -> None:
    # resolve() keeps these spellings, so they would get past the home check.
    set_home(tmp_path)
    home = str(tmp_path)
    env.setenv("AGENT_BRIDGE_ALLOWED_ROOTS", prefix + (home if prefix == "\\\\?\\" else home[3:]))
    with pytest.raises(RuntimeError, match="must be on a drive"):
        Settings.from_env()
