"""herdr argv and subprocess handling that differs on Windows. These run on every
platform: Windows' rules are applied by setting `herdr.WINDOWS`."""

import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_bridge import herdr
from agent_bridge.app import create_app
from agent_bridge.config import Settings
from agent_bridge.herdr import Herdr, HerdrError, command_line_length, subprocess_runner

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
# Where herdr's CLI lands on Windows.
HERDR_EXE = r"C:\Users\me\AppData\Local\Programs\Herdr\bin\herdr.exe"


def ok(result: dict) -> tuple[int, str, str]:
    return 0, json.dumps({"id": "cli", "result": result}), ""


def err(code: str) -> tuple[int, str, str]:
    return 1, "", json.dumps({"id": "cli", "error": {"code": code, "message": code}})


def idle(cwd: str) -> dict:
    return {"agent": "claude", "agent_status": "idle", "cwd": cwd, "pane_id": "w1:p1",
            "workspace_id": "w1", "name": "probe"}


class Recorder:
    """Answers every herdr call by its first two arguments, and keeps each argv."""

    def __init__(self, responses: dict[tuple[str, str], tuple[int, str, str]]):
        self.responses = responses
        self.calls: list[list[str]] = []

    async def __call__(self, argv: Sequence[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        return self.responses[tuple(argv[1:3])]


@pytest.fixture
def on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(herdr, "WINDOWS", True)


def test_counts_the_command_line_as_windows_quotes_it() -> None:
    assert command_line_length(["herdr", "a b"]) == len('herdr "a b"')
    assert command_line_length(["herdr", '"']) == len('herdr \\"')
    # An emoji is two UTF-16 units.
    assert command_line_length(["herdr", "🙂"]) == len("herdr ") + 2


@pytest.mark.parametrize("text", ['"' * 20_000, "🙂" * 20_000, '\\"' * 10_000])
@pytest.mark.asyncio
async def test_refuses_a_prompt_too_long_for_a_windows_command_line(
    on_windows: None, text: str
) -> None:
    runner = Recorder({})
    with pytest.raises(HerdrError) as raised:
        await Herdr(binary=HERDR_EXE, runner=runner).prompt("w1:p1", text, 600_000)
    assert raised.value.code == "prompt_too_long"
    assert "shorter parts" in raised.value.message
    assert runner.calls == []


@pytest.mark.asyncio
async def test_the_longest_prompt_allowed_goes_through_when_plain(on_windows: None) -> None:
    runner = Recorder({("agent", "prompt"): ok({"agent": idle("C:/p")})})
    await Herdr(binary=HERDR_EXE, runner=runner).prompt("w1:p1", "ż" * 20_000, 600_000)
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_only_windows_limits_the_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(herdr, "WINDOWS", False)
    runner = Recorder({("agent", "prompt"): ok({"agent": idle("/p")})})
    await Herdr(runner=runner).prompt("w1:p1", '"' * 20_000, 600_000)
    assert len(runner.calls) == 1


async def command_line_too_long(argv: Sequence[str]) -> tuple[int, str, str]:
    error = FileNotFoundError(2, "The filename or extension is too long")
    error.winerror = 206  # type: ignore[attr-defined]  # set by Windows itself
    raise error


@pytest.mark.asyncio
async def test_a_command_line_windows_refuses_is_not_reported_as_herdr_missing() -> None:
    with pytest.raises(HerdrError) as raised:
        await Herdr(binary=HERDR_EXE, runner=command_line_too_long).list_agents()
    assert raised.value.code == "command_too_long"


def test_a_prompt_too_long_for_windows_is_the_callers_to_shorten(
    on_windows: None, tmp_path: Path
) -> None:
    runner = Recorder({("agent", "get"): ok({"agent": idle(str(tmp_path))})})
    settings = Settings(token=TOKEN, allowed_roots=(tmp_path.resolve(),), herdr_bin=HERDR_EXE)
    app = create_app(settings, Herdr(binary=HERDR_EXE, runner=runner))
    response = TestClient(app).post(
        "/agents/w1:p1/prompt", headers=AUTH, json={"text": '"' * 20_000}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "prompt_too_long"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own command-line limit")
@pytest.mark.asyncio
async def test_the_limit_is_where_windows_puts_it() -> None:
    # A real child process: a command line of exactly the limit runs, one unit more doesn't.
    # The base interpreter: a venv's python.exe starts it again with a longer command line.
    argv = [sys._base_executable, "-c", "pass"]
    fits = herdr.WINDOWS_COMMAND_LINE_MAX - command_line_length(argv) - len(" ")
    assert (await subprocess_runner([*argv, "x" * fits]))[0] == 0
    with pytest.raises(FileNotFoundError) as raised:
        await subprocess_runner([*argv, "x" * (fits + 1)])
    assert raised.value.winerror == 206


START = {
    ("workspace", "create"): ok({"workspace": {"workspace_id": "w5"}, "root_pane": {"pane_id": "w5:p1"}}),
    ("workspace", "close"): ok({"type": "ok"}),
}


@pytest.mark.asyncio
async def test_a_start_blocked_on_the_trust_dialog_is_an_untrusted_folder() -> None:
    # herdr on Windows calls an agent on the trust dialog blocked, so `agent start` fails
    # with agent_not_ready before the bridge's own check for the dialog.
    runner = Recorder({
        **START,
        ("agent", "start"): err("agent_not_ready"),
        ("pane", "wait-output"): ok({"matched_line": "Yes, I trust this folder"}),
    })
    with pytest.raises(HerdrError) as raised:
        await Herdr(runner=runner).start_agent("probe", "C:/p", {}, ())
    assert raised.value.code == "folder_not_trusted"
    assert runner.calls[-1][1:] == ["workspace", "close", "w5"]


@pytest.mark.asyncio
async def test_a_start_not_ready_for_another_reason_stays_not_ready() -> None:
    runner = Recorder({
        **START,
        ("agent", "start"): err("agent_not_ready"),
        ("pane", "wait-output"): err("timeout"),
    })
    with pytest.raises(HerdrError) as raised:
        await Herdr(runner=runner).start_agent("probe", "C:/p", {}, ())
    assert raised.value.code == "agent_not_ready"
    assert runner.calls[-1][1:] == ["workspace", "close", "w5"]


@pytest.mark.asyncio
async def test_a_failing_dialog_check_keeps_the_reason_the_start_failed() -> None:
    runner = Recorder({
        **START,
        ("agent", "start"): err("agent_not_ready"),
        ("pane", "wait-output"): err("pane_not_found"),
    })
    with pytest.raises(HerdrError) as raised:
        await Herdr(runner=runner).start_agent("probe", "C:/p", {}, ())
    assert raised.value.code == "agent_not_ready"
    assert runner.calls[-1][1:] == ["workspace", "close", "w5"]
