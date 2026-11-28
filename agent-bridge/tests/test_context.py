import io
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_bridge import context
from agent_bridge.context import context_use, encode, read_context, save
from agent_bridge.install import statusline

SESSION = "145550d5-22e8-42fc-8263-0bd7b29cddb9"


def status(session_id: str = SESSION, **window) -> dict:
    """A status line input as Claude Code sends it, cut to what the relay reads."""
    usage = {"input_tokens": 12, "output_tokens": 500, "cache_creation_input_tokens": 1_000,
             "cache_read_input_tokens": 339_000}
    return {"session_id": session_id, "model": {"id": "claude-opus-5-5"},
            "context_window": {"context_window_size": 1_000_000, "used_percentage": 34.5,
                               "total_input_tokens": 340_012, "current_usage": usage, **window}}


# --- What a saved window says -----------------------------------------------------------


def test_context_use_is_claude_codes_own_percentage_with_the_tokens_in_the_window() -> None:
    assert context_use(status()) == (34, 340_012, 1_000_000)


def test_a_percentage_not_given_yet_is_worked_out() -> None:
    assert context_use(status(used_percentage=None)) == (34, 340_012, 1_000_000)


def test_without_current_usage_the_total_input_counts() -> None:
    assert context_use(status(current_usage=None, total_input_tokens=50_000, used_percentage=None)) == (
        5, 50_000, 1_000_000)


@pytest.mark.parametrize(
    "saved",
    [
        # Right after /compact: current_usage is zeros and nothing else is known.
        status(current_usage={"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}, total_input_tokens=0, used_percentage=None),
        status(context_window_size=None),
        status(context_window_size=0),
        {"context_window": None},
        {"context_window": "full"},
        {},
        [],
        None,
    ],
)
def test_a_window_that_wasnt_measured_is_unknown(saved: object) -> None:
    assert context_use(saved) is None


def test_odd_numbers_never_make_a_percentage() -> None:
    saved = status(used_percentage=True, current_usage={"input_tokens": "12"}, total_input_tokens=1_000)
    assert context_use(saved) == (0, 1_000, 1_000_000)
    assert context_use(status(used_percentage=250)) == (100, 340_012, 1_000_000)


# --- Saving and reading ---------------------------------------------------------------


def test_a_saved_status_is_read_back(tmp_path: Path) -> None:
    save(json.dumps(status()).encode(), tmp_path)
    assert read_context(SESSION, [tmp_path / "other", tmp_path]) == (34, 340_012, 1_000_000)
    assert json.loads((tmp_path / "agent-bridge" / "context" / f"{SESSION}.json").read_text())["model"] == (
        "claude-opus-5-5")


def test_the_newest_of_several_configs_wins(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    save(json.dumps(status(used_percentage=10)).encode(), old)
    save(json.dumps(status(used_percentage=60)).encode(), new)
    os.utime(old / "agent-bridge" / "context" / f"{SESSION}.json", (1, 1))
    assert read_context(SESSION, [old, new])[0] == 60
    assert read_context(SESSION, [new, old])[0] == 60


@pytest.mark.parametrize("session_id", ["../escape", "a/b", "", "x" * 65, None, 7])
def test_session_ids_that_arent_ids_are_never_files(tmp_path: Path, session_id: object) -> None:
    save(json.dumps(status(session_id=session_id)).encode(), tmp_path)
    assert not (tmp_path / "agent-bridge").exists()
    assert read_context(session_id if isinstance(session_id, str) else None, [tmp_path]) is None


def test_a_session_nobody_saved_is_unknown(tmp_path: Path) -> None:
    assert read_context(SESSION, [tmp_path]) is None
    assert read_context(None, [tmp_path]) is None


def test_a_broken_file_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "agent-bridge" / "context" / f"{SESSION}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert read_context(SESSION, [tmp_path]) is None


# --- The relay ------------------------------------------------------------------------


def run_relay(monkeypatch: pytest.MonkeyPatch, config: Path, data: bytes, *args: str) -> int:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))
    return context.main(list(args))


def python_command(code: str) -> str:
    """A shell command running `code` with this Python, the way a status line command would."""
    return f"'{Path(sys.executable).as_posix()}' -c \"{code}\""


# Windows without Git Bash runs status lines in PowerShell, which these commands aren't for.
needs_bash = pytest.mark.skipif(context.shell("x")[0].endswith("powershell.exe"), reason="no Git Bash")


@needs_bash
def test_the_relay_saves_then_runs_the_status_line_with_the_same_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    data = json.dumps(status()).encode()
    show = python_command("import sys, json; print(json.load(sys.stdin)['model']['id'])")
    assert run_relay(monkeypatch, tmp_path, data, "--then", encode(show)) == 0
    assert capfd.readouterr().out.strip() == "claude-opus-5-5"
    assert read_context(SESSION, [tmp_path]) == (34, 340_012, 1_000_000)


@needs_bash
def test_the_relay_passes_the_status_lines_exit_code_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = json.dumps(status()).encode()
    assert run_relay(monkeypatch, tmp_path, data, "--then", encode(python_command("raise SystemExit(3)"))) == 3


def test_without_a_status_line_the_relay_only_saves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run_relay(monkeypatch, tmp_path, json.dumps(status()).encode()) == 0
    assert capfd.readouterr().out == ""
    assert read_context(SESSION, [tmp_path]) is not None


@needs_bash
def test_input_that_cant_be_saved_still_shows_the_status_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run_relay(monkeypatch, tmp_path, b"not json", "--then", encode(python_command("print('shown')"))) == 0
    assert capfd.readouterr().out.strip() == "shown"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells")
def test_the_installed_relay_runs_as_a_command(tmp_path: Path) -> None:
    script = Path(sys.executable).with_name("agent-bridge-statusline")
    if not script.exists():
        pytest.skip("agent-bridge-statusline isn't installed in this environment")
    show = "cat >/dev/null; echo shown"
    result = subprocess.run(f"{statusline.relay_word(script)} --then {encode(show)}", shell=True,
                            input=json.dumps(status()).encode(), capture_output=True,
                            env={**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)})
    assert (result.returncode, result.stdout) == (0, b"shown\n")
    assert read_context(SESSION, [tmp_path]) == (34, 340_012, 1_000_000)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells")
def test_the_status_line_runs_in_bash_when_there_is_one() -> None:
    assert context.shell("echo hi")[1:] == ["-c", "echo hi"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' shells")
def test_on_windows_without_git_bash_powershell_runs_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_GIT_BASH_PATH", raising=False)
    monkeypatch.setattr(context.shutil, "which", lambda name: None)
    assert context.shell("echo hi")[0] == "powershell.exe"


# --- The relay in settings.json -------------------------------------------------------


RELAY = Path("/opt/bin/agent-bridge-statusline")
WRAPPED = "bash ~/sl.sh 'x'"


def settings(config: Path) -> dict:
    return json.loads((config / "settings.json").read_text(encoding="utf-8"))


def write_settings(config: Path, value: dict) -> None:
    config.mkdir(parents=True, exist_ok=True)
    (config / "settings.json").write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(statusline.sys, "platform", "darwin")


def test_install_wraps_the_status_line_and_remove_puts_it_back(tmp_path: Path, posix: None) -> None:
    config = tmp_path / ".claude-agents"
    write_settings(config, {"statusLine": {"type": "command", "command": WRAPPED, "padding": 0}, "model": "opus"})
    assert statusline.install(config, RELAY) == "written"
    status_line = settings(config)["statusLine"]
    assert status_line == {"type": "command", "command": f"{RELAY} --then {encode(WRAPPED)}", "padding": 0}
    assert statusline.install(config, RELAY) == "already"
    assert statusline.remove(config)
    assert settings(config) == {"statusLine": {"type": "command", "command": WRAPPED, "padding": 0}, "model": "opus"}
    assert not statusline.remove(config)


def test_a_config_without_a_status_line_gets_only_the_relay_and_loses_it_again(tmp_path: Path, posix: None) -> None:
    config = tmp_path / ".claude-agents"
    write_settings(config, {"model": "opus"})
    statusline.install(config, RELAY)
    assert settings(config)["statusLine"] == {"type": "command", "command": str(RELAY)}
    assert statusline.remove(config)
    assert settings(config) == {"model": "opus"}


def test_a_config_without_settings_gets_them(tmp_path: Path, posix: None) -> None:
    config = tmp_path / ".claude-agents"
    assert statusline.install(config, RELAY) == "written"
    assert settings(config) == {"statusLine": {"type": "command", "command": str(RELAY)}}


def test_a_moved_relay_is_rewritten_keeping_the_wrapped_command(tmp_path: Path, posix: None) -> None:
    config = tmp_path / ".claude-agents"
    write_settings(config, {"statusLine": {"type": "command", "command": "echo hi"}})
    statusline.install(config, RELAY)
    moved = Path("/new place/agent-bridge-statusline")
    assert statusline.install(config, moved) == "written"
    assert settings(config)["statusLine"]["command"] == f"'{moved}' --then {encode('echo hi')}"
    assert statusline.unwrapped(settings(config)["statusLine"]["command"]) == (True, "echo hi")


@pytest.mark.parametrize(
    "command",
    ["echo agent-bridge-statusline", "bash -c 'agent-bridge-statusline'", "my-agent-bridge-statusline-x",
     "/opt/bin/agent-bridge-statusline --then", "/opt/bin/agent-bridge-statusline; rm -rf x"],
)
def test_commands_that_only_mention_the_relay_arent_its(command: str) -> None:
    assert statusline.unwrapped(command) == (False, None)


def test_windows_paths_are_one_word_in_git_bash_and_powershell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(statusline.sys, "platform", "win32")
    relay = Path("C:/Users/jan/.local/bin/agent-bridge-statusline.exe")
    assert statusline.relay_word(relay) == "C:/Users/jan/.local/bin/agent-bridge-statusline.exe"
    assert statusline.unwrapped(f"{statusline.relay_word(relay)} --then {encode('x')}") == (True, "x")
    monkeypatch.setattr(statusline, "_short_path", lambda path: "C:/Users/JANKOW~1/.local/bin/AGENT-~1.EXE")
    assert statusline.relay_word(Path("C:/Users/Jan Kowalski/.local/bin/agent-bridge-statusline.exe")) == (
        "C:/Users/JANKOW~1/.local/bin/AGENT-~1.EXE")
    monkeypatch.setattr(statusline, "_short_path", lambda path: path)
    with pytest.raises(ValueError):
        statusline.relay_word(Path("C:/Users/Jan Kowalski/.local/bin/agent-bridge-statusline.exe"))


@pytest.mark.parametrize(
    ("content", "message"),
    [("{broken", "not valid JSON"), ("[]", "not a JSON object"),
     (json.dumps({"statusLine": {"type": "static"}}), "isn't a command"),
     (json.dumps({"statusLine": {"type": "command", "command": 5}}), "isn't text")],
)
def test_settings_it_cant_wrap_are_left_alone(tmp_path: Path, posix: None, content: str, message: str) -> None:
    config = tmp_path / ".claude-agents"
    config.mkdir()
    (config / "settings.json").write_text(content)
    with pytest.raises(ValueError, match=message):
        statusline.install(config, RELAY)
    assert (config / "settings.json").read_text() == content


def test_the_default_config_is_never_wrapped(set_home: Callable[[Path], None], tmp_path: Path) -> None:
    set_home(tmp_path)
    with pytest.raises(ValueError, match="default Claude config"):
        statusline.install(tmp_path / ".claude", RELAY)
    assert not (tmp_path / ".claude").exists()
