"""herdr's resume setting in its config file, and herdr's Claude hook in a Claude config."""

import json
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_bridge.install.herdr_config import (
    MARKER,
    has_claude_hook,
    herdr_config_paths,
    hook_config_dirs,
    turn_off_resume,
    undo_resume,
)

ORIGINAL = """\
# herdr settings
theme = "dark"

[ui]
sidebar = true # shown

[session]
autosave = true

[keys]
prefix = "ctrl-b"
"""


def resume(path: Path) -> object:
    return tomllib.loads(path.read_text(encoding="utf-8")).get("session", {}).get("resume_agents_on_restore")


def test_writes_under_existing_session_table_and_keeps_rest(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(ORIGINAL, encoding="utf-8")

    assert turn_off_resume(path) == "written"

    assert path.read_text(encoding="utf-8") == ORIGINAL.replace(
        "[session]\n", f"[session]\n{MARKER}\nresume_agents_on_restore = false\n")
    assert resume(path) is False


def test_keeps_windows_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = ORIGINAL.replace("\n", "\r\n").encode()
    path.write_bytes(original)

    turn_off_resume(path)

    assert path.read_bytes() == original.replace(
        b"[session]\r\n", f"[session]\r\n{MARKER}\r\nresume_agents_on_restore = false\r\n".encode())


def test_appends_session_table_when_missing(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = '[ui]\nsidebar = true'
    path.write_text(original, encoding="utf-8")

    assert turn_off_resume(path) == "written"

    assert path.read_text(encoding="utf-8") == (
        f"{original}\n\n[session]\n{MARKER}\nresume_agents_on_restore = false\n")
    assert resume(path) is False


def test_creates_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "herdr" / "config.toml"

    assert turn_off_resume(path) == "written"

    assert path.read_text(encoding="utf-8") == f"[session]\n{MARKER}\nresume_agents_on_restore = false\n"


def test_edits_a_linked_config_where_it_lives(tmp_path: Path, symlink: Callable[[Path, Path], None]) -> None:
    target = tmp_path / "dotfiles" / "config.toml"
    target.parent.mkdir()
    target.write_text(ORIGINAL, encoding="utf-8")
    path = tmp_path / "config.toml"
    symlink(path, target)

    turn_off_resume(path)

    assert path.is_symlink()
    assert resume(target) is False


@pytest.mark.parametrize("value", ["true", "false"])
def test_user_setting_is_left_alone(tmp_path: Path, value: str) -> None:
    path = tmp_path / "config.toml"
    original = ORIGINAL.replace("autosave = true\n", f"autosave = true\nresume_agents_on_restore = {value}\n")
    path.write_text(original, encoding="utf-8")

    assert turn_off_resume(path) == "yours"

    assert path.read_text(encoding="utf-8") == original


def test_user_setting_as_a_dotted_key_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = "session.resume_agents_on_restore = true\n"
    path.write_text(original, encoding="utf-8")

    assert turn_off_resume(path) == "yours"
    assert path.read_text(encoding="utf-8") == original


def test_second_run_is_already(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(ORIGINAL, encoding="utf-8")
    turn_off_resume(path)
    written = path.read_bytes()

    assert turn_off_resume(path) == "already"

    assert path.read_bytes() == written


def test_a_config_it_cant_read_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[session\n", encoding="utf-8")

    with pytest.raises(ValueError):
        turn_off_resume(path)

    assert path.read_text(encoding="utf-8") == "[session\n"


@pytest.mark.parametrize("original", [ORIGINAL, "[ui]\nsidebar = true\n", ""])
def test_undo_removes_only_our_lines(tmp_path: Path, original: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(original, encoding="utf-8")
    turn_off_resume(path)

    assert undo_resume(path)

    assert path.read_text(encoding="utf-8") == original


def test_undo_leaves_user_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    # Our marker, but the line after it is the user's now.
    original = ORIGINAL.replace("[session]\n", f"[session]\n{MARKER}\nresume_agents_on_restore = true\n")
    path.write_text(original, encoding="utf-8")

    assert not undo_resume(path)

    assert path.read_text(encoding="utf-8") == original


def test_undo_without_a_file_does_nothing(tmp_path: Path) -> None:
    assert not undo_resume(tmp_path / "config.toml")
    assert not (tmp_path / "config.toml").exists()


def test_herdr_config_path_alone_names_the_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERDR_CONFIG_PATH", str(tmp_path / "herdr.toml"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert herdr_config_paths() == [tmp_path / "herdr.toml"]


def test_the_config_path_defaults_to_herdrs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                            set_home: Callable[[Path], None]) -> None:
    monkeypatch.delenv("HERDR_CONFIG_PATH", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    set_home(tmp_path)

    expected = (tmp_path / "AppData" / "Roaming" if sys.platform == "win32" else tmp_path / ".config")
    assert herdr_config_paths() == [expected / "herdr" / "config.toml"]


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_xdg_config_home_elsewhere_names_both_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                    set_home: Callable[[Path], None], platform: str) -> None:
    # herdr honours XDG_CONFIG_HOME on Windows too, instead of %APPDATA%.
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.delenv("HERDR_CONFIG_PATH", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "home" / "AppData" / "Roaming"))
    set_home(tmp_path / "home")

    default = tmp_path / "home" / ("AppData/Roaming" if platform == "win32" else ".config")
    assert herdr_config_paths() == [tmp_path / "xdg" / "herdr" / "config.toml",
                                    default / "herdr" / "config.toml"]


@pytest.mark.skipif(sys.platform == "win32", reason="~/.config isn't herdr's default on Windows")
@pytest.mark.parametrize("xdg", ["{home}/.config", "{home}/.config/", "relative/config"])
def test_xdg_config_home_at_the_default_or_relative_names_one_file(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, set_home: Callable[[Path], None], xdg: str) -> None:
    monkeypatch.delenv("HERDR_CONFIG_PATH", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", xdg.format(home=tmp_path))
    set_home(tmp_path)

    assert herdr_config_paths() == [tmp_path / ".config" / "herdr" / "config.toml"]


def hooked(config_dir: Path, script_dir: Path) -> Path:
    """A Claude config with herdr's SessionStart hook, its script in `script_dir`."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": "^(startup|resume|clear|compact|fork)$", "hooks": [
            {"type": "command", "command": f"bash '{script_dir}/hooks/herdr-agent-state.sh' session", "timeout": 10},
        ]},
    ]}}), encoding="utf-8")
    return config_dir


def test_has_claude_hook_matches_foreign_script_path(tmp_path: Path) -> None:
    assert has_claude_hook(hooked(tmp_path / "work", tmp_path / "personal"))


@pytest.mark.parametrize("settings", [
    None, "not json", "[]", '{"hooks": null}', '{"hooks": {"SessionStart": [{"hooks": [{"command": "true"}]}]}}',
    '{"hooks": {"Stop": [{"hooks": [{"command": "herdr-agent-state.sh"}]}]}}',
])
def test_has_claude_hook_without_it(tmp_path: Path, settings: str | None) -> None:
    if settings is not None:
        (tmp_path / "settings.json").write_text(settings, encoding="utf-8")

    assert not has_claude_hook(tmp_path)


def test_hook_config_dirs_skips_default(tmp_path: Path, set_home: Callable[[Path], None]) -> None:
    set_home(tmp_path)
    agents, work = tmp_path / ".claude-agents", tmp_path / ".claude-work"
    accounts = {"personal": {"config_dir": None}, "default": {"config_dir": str(tmp_path / ".claude")},
                "work": {"config_dir": str(work), "folders": [str(tmp_path)]}, "agents": {"config_dir": str(agents)}}
    values = {"AGENT_BRIDGE_CLAUDE_CONFIG_DIR": str(agents), "AGENT_BRIDGE_ACCOUNTS": json.dumps(accounts)}

    assert hook_config_dirs(values) == [agents, work]


def test_hook_config_dirs_without_any(tmp_path: Path) -> None:
    assert hook_config_dirs({"AGENT_BRIDGE_CLAUDE_CONFIG_DIR": ""}) == []


@pytest.mark.parametrize("accounts", ["{not json", "[1]", '{"work": "/cfg"}'])
def test_hook_config_dirs_with_malformed_accounts_warns_and_goes_on(
    tmp_path: Path, capsys: pytest.CaptureFixture, accounts: str
) -> None:
    agents = tmp_path / ".claude-agents"
    values = {"AGENT_BRIDGE_CLAUDE_CONFIG_DIR": str(agents), "AGENT_BRIDGE_ACCOUNTS": accounts}

    assert hook_config_dirs(values) == [agents]
    assert "warning: AGENT_BRIDGE_ACCOUNTS" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX system runs a shell script as herdr")
def test_posix_herdr_integration_passes_the_config_dir(tmp_path: Path) -> None:
    from agent_bridge.install.flow import PosixSystem

    record = tmp_path / "seen"
    herdr = tmp_path / "herdr"
    # A stand-in herdr: records its arguments and CLAUDE_CONFIG_DIR, and prints a byte that
    # isn't UTF-8, as a localized message might.
    herdr.write_text(f'#!/bin/sh\necho "$@ $CLAUDE_CONFIG_DIR" > "{record}"\nprintf "done \\377\\n"\n')
    herdr.chmod(0o755)

    code, output = PosixSystem().herdr_integration(str(herdr), tmp_path / "cfg")

    assert code == 0
    assert record.read_text() == f"integration install claude {tmp_path / 'cfg'}\n"
    assert output.startswith("done ")


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX system runs a shell script as herdr")
def test_posix_herdr_integration_reports_a_missing_herdr(tmp_path: Path) -> None:
    from agent_bridge.install.flow import FAILED, PosixSystem

    code, output = PosixSystem().herdr_integration(str(tmp_path / "no-herdr"), tmp_path / "cfg")

    assert code == FAILED
    assert output


def test_a_session_key_that_isnt_a_table_is_refused_as_not_herdrs_config(tmp_path: Path) -> None:
    # Valid TOML, but herdr wants a table there: setup warns and goes on, as for broken TOML.
    path = tmp_path / "config.toml"
    path.write_text("session = 1\n", encoding="utf-8")

    with pytest.raises(ValueError):
        turn_off_resume(path)
    assert path.read_text(encoding="utf-8") == "session = 1\n"
