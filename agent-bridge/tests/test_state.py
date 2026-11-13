import os
import sys
from pathlib import Path

import pytest

from agent_bridge import state
from agent_bridge.state import Gateway, State, StateError, env_file, load, load_or_create, save, state_file


@pytest.fixture
def defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, set_home) -> Path:
    """No overrides, a home folder of its own, and Windows' folders under it."""
    monkeypatch.delenv("AGENT_BRIDGE_ENV_FILE")
    monkeypatch.delenv("AGENT_BRIDGE_STATE_FILE")
    set_home(tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    return tmp_path


@pytest.mark.parametrize(
    ("platform", "settings", "state_path"),
    [
        ("darwin", "Library/Application Support/agent-bridge/env",
         "Library/Application Support/agent-bridge/state.json"),
        ("linux", ".config/agent-bridge/env", ".local/state/agent-bridge/state.json"),
        ("win32", "Roaming/agent-bridge/env", "Local/agent-bridge/state.json"),
    ],
)
def test_default_paths_per_platform(
    defaults: Path, monkeypatch: pytest.MonkeyPatch, platform: str, settings: str, state_path: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform)

    assert env_file() == defaults / settings
    assert state_file() == defaults / state_path


def test_windows_folders_without_their_variables(defaults: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("APPDATA")
    monkeypatch.delenv("LOCALAPPDATA")

    assert env_file() == defaults / "AppData" / "Roaming" / "agent-bridge" / "env"
    assert state_file() == defaults / "AppData" / "Local" / "agent-bridge" / "state.json"


def test_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENT_BRIDGE_ENV_FILE", str(tmp_path / "my-env"))
    monkeypatch.setenv("AGENT_BRIDGE_STATE_FILE", str(tmp_path / "my-state.json"))

    assert (env_file(), state_file()) == (tmp_path / "my-env", tmp_path / "my-state.json")


def test_state_is_made_once_with_a_bridge_id(tmp_path: Path) -> None:
    path = tmp_path / "folder" / "state.json"
    first = load_or_create(path)

    assert len(first.bridge_id) == 32
    assert load_or_create(path) == first
    assert load(path) == first


@pytest.mark.skipif(os.name != "posix", reason="Windows keeps the file private by its folder")
def test_state_is_readable_only_by_the_user(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    # A leftover temporary file with a looser mode doesn't pass it on.
    (tmp_path / "state.json.tmp").write_text("x")
    (tmp_path / "state.json.tmp").chmod(0o644)

    save(path, State(bridge_id="1" * 32))

    assert path.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "state.json.tmp").exists()


def test_a_failed_write_keeps_the_old_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.json"
    save(path, State(bridge_id="1" * 32))

    def fail(*args) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(state.os, "replace", fail)
    with pytest.raises(StateError, match="No space left"):
        save(path, State(bridge_id="1" * 32, gateway=Gateway(url="http://g", gateway_id="g", name="mac")))

    assert load(path) == State(bridge_id="1" * 32)
    assert not (tmp_path / "state.json.tmp").exists()


def test_unreadable_state_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json")

    with pytest.raises(StateError, match="isn't agent-bridge state"):
        load_or_create(path)
