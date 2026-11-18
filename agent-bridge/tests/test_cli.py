"""`agent-bridge serve | enroll | unenroll | check`, against a fake gateway: the exit codes
and files the platform setups rely on."""

import asyncio
import json
import logging
import os
import re
import sys
import tomllib
from pathlib import Path

import httpx
import pytest

from agent_bridge import cli, gateway
from agent_bridge.config import Settings
from agent_bridge.state import Gateway, State, load, save

TOKEN = "t" * 32
STRING = "crm1@192.168.1.10:8766#" + "a" * 24
GATEWAY = "http://192.168.1.10:8766"


class FakeGateway:
    """Answers the gateway's routes from a table, a list being answered in order; no
    answer means unreachable."""

    def __init__(self) -> None:
        self.answers: dict[tuple[str, str], httpx.Response | list[httpx.Response]] = {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path.split("/")[1])
        answer = self.answers.get(key)
        if isinstance(answer, list):
            answer = answer.pop(0)
        if answer is None:
            raise httpx.ConnectError("connection refused", request=request)
        return answer

    def body(self) -> dict:
        return json.loads(self.requests[-1].content)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    fake = FakeGateway()
    real = httpx.AsyncClient
    monkeypatch.setattr(gateway.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(fake), **kwargs))
    return fake


@pytest.fixture
def settings(tmp_path: Path) -> Path:
    """A complete env file, as the platform setups write it."""
    path = Path(os.environ["AGENT_BRIDGE_ENV_FILE"])
    path.write_text(
        f"AGENT_BRIDGE_TOKEN={TOKEN}\nAGENT_BRIDGE_ALLOWED_ROOTS={tmp_path}\nAGENT_BRIDGE_HOST=192.168.1.20\n"
        "AGENT_BRIDGE_PORT=8765\nAGENT_BRIDGE_NAME=mac\nAGENT_BRIDGE_HERDR_BIN=/opt/herdr\n"
    )
    path.chmod(0o600)
    return path


def state_path() -> Path:
    return Path(os.environ["AGENT_BRIDGE_STATE_FILE"])


def run(capsys: pytest.CaptureFixture, *args: str, **kwargs) -> tuple[int, str, str]:
    code = cli.main(list(args), **kwargs)
    out, err = capsys.readouterr()
    return code, out, err


def enrolled() -> State:
    state = State(bridge_id="1" * 32, gateway=Gateway(url=GATEWAY, gateway_id="g" * 32, name="mac"))
    save(state_path(), state)
    return state


@pytest.mark.parametrize(
    ("flag", "value", "code", "reason"),
    [
        ("--token-format", "a" * 64, 0, ""),
        ("--token-format", "short", 2, "at least 32"),
        ("--token-format", "a" * 31 + "$", 2, "may only have"),
        ("--name-format", "linux-box", 0, ""),
        ("--name-format", "Linux Box", 2, "lowercase letter"),
    ],
)
def test_check_formats(capsys, flag: str, value: str, code: int, reason: str) -> None:
    result, out, err = run(capsys, "check", flag, value)

    assert result == code
    assert out == ""
    assert reason in err


def test_version_is_the_packages(capsys) -> None:
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())

    with pytest.raises(SystemExit) as exit_:
        cli.main(["--version"])

    assert exit_.value.code == 0
    assert capsys.readouterr().out == f"agent-bridge {pyproject['project']['version']}\n"


def test_serve_runs_from_the_env_file_and_makes_the_state(capsys, settings: Path) -> None:
    served = {}

    def serve(app, **options) -> None:
        served.update(options, app=app)

    assert run(capsys, "serve", run=serve)[0] == 0
    assert (served["host"], served["port"]) == ("192.168.1.20", 8765)
    state = load(state_path())
    assert state is not None and served["app"].state.agents.bridge_id == state.bridge_id


def test_serve_refuses_without_what_the_service_needs(capsys, settings: Path) -> None:
    settings.write_text(f"AGENT_BRIDGE_TOKEN={TOKEN}\nAGENT_BRIDGE_ALLOWED_ROOTS={settings.parent}\n")

    code, _, err = run(capsys, "serve", run=lambda app, **options: pytest.fail("served"))

    assert code == 1
    assert "AGENT_BRIDGE_HOST, AGENT_BRIDGE_NAME, AGENT_BRIDGE_HERDR_BIN" in err
    assert str(settings) in err


def test_serve_reads_the_env_file_as_utf8(capsys, settings: Path, tmp_path: Path) -> None:
    # As the Windows setup writes it, whatever the locale's code page is.
    root = tmp_path / "Projekty-żółw"
    root.mkdir()
    settings.write_text(settings.read_text().replace(f"={tmp_path}\n", f"={root}\n"), encoding="utf-8")
    served = {}

    assert run(capsys, "serve", run=lambda app, **options: served.update(app=app))[0] == 0
    assert served["app"].state.agents.settings.allowed_roots == (root.resolve(),)


def test_serve_logs_to_the_log_file_only_what_it_logs(capsys, settings: Path, tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "agent-bridge.log"
    log_file.parent.mkdir()
    log_file.write_text("an earlier start\n", encoding="utf-8")
    served = {}

    def serve(app, **options) -> None:
        served.update(options)
        logging.getLogger("uvicorn.error").info("Uvicorn running on http://192.168.1.20:8765")
        print("printed: may hold a setting", file=sys.stderr)

    code, _, err = run(capsys, "serve", "--log-file", str(log_file), run=serve)

    assert code == 0
    # uvicorn's loggers are left to reach the root logger's file.
    assert served["log_config"] is None
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "an earlier start"
    assert re.fullmatch(r"==== \d{4}-\d\d-\d\d \d\d:\d\d:\d\d starting agent-bridge", lines[1])
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d INFO      Uvicorn running on http://192.168.1.20:8765", lines[2])
    assert "printed" not in log_file.read_text(encoding="utf-8")
    assert "Uvicorn running" not in err
    # Let go of once serving ends.
    assert str(log_file) not in [getattr(handler, "baseFilename", "") for handler in logging.getLogger().handlers]


def test_serve_sets_a_large_log_aside_first(capsys, settings: Path, tmp_path: Path) -> None:
    log_file = tmp_path / "agent-bridge.log"
    log_file.write_bytes(b"x" * (cli.LOG_LIMIT + 1))
    (tmp_path / "agent-bridge.log.1").write_text("the one before", encoding="utf-8")

    assert run(capsys, "serve", "--log-file", str(log_file), run=lambda app, **options: None)[0] == 0
    assert (tmp_path / "agent-bridge.log.1").stat().st_size == cli.LOG_LIMIT + 1
    assert log_file.read_text(encoding="utf-8").startswith("==== ")


def test_serve_sets_the_log_aside_while_it_runs(capsys, settings: Path, tmp_path: Path, monkeypatch) -> None:
    # A host that isn't restarted for weeks.
    monkeypatch.setattr(cli, "LOG_LIMIT", 2000)
    log_file = tmp_path / "agent-bridge.log"
    sizes = []

    def serve(app, **options) -> None:
        for number in range(100):
            logging.getLogger("uvicorn.access").info("request %d", number)
            sizes.append(log_file.stat().st_size)

    assert run(capsys, "serve", "--log-file", str(log_file), run=serve)[0] == 0
    assert max(sizes) <= 2000
    assert "request 99" in log_file.read_text(encoding="utf-8")
    assert "request" in (tmp_path / "agent-bridge.log.1").read_text(encoding="utf-8")


def test_serve_keeps_logging_while_the_log_cant_be_moved(capsys, settings: Path, tmp_path: Path, monkeypatch) -> None:
    # On Windows, while another process has it open, e.g. Get-Content -Wait.
    monkeypatch.setattr(cli, "LOG_LIMIT", 500)
    log_file = tmp_path / "agent-bridge.log"
    backup = tmp_path / "agent-bridge.log.1"
    backup.write_text("an older line\n", encoding="utf-8")
    replace = os.replace
    holding = True

    def held(source, dest) -> None:
        if holding and Path(source) == log_file:
            raise PermissionError(13, "The process cannot access the file")
        replace(source, dest)

    def serve(app, **options) -> None:
        nonlocal holding
        monkeypatch.setattr(os, "replace", held)
        monkeypatch.setattr(os, "rename", held)
        for number in range(30):
            logging.getLogger("uvicorn.access").info("request %d", number)
        # The last move is kept until this one can be made.
        assert backup.read_text(encoding="utf-8") == "an older line\n"
        holding = False
        logging.getLogger("uvicorn.access").info("after it was let go")

    assert run(capsys, "serve", "--log-file", str(log_file), run=serve)[0] == 0
    lines = (tmp_path / "agent-bridge.log.1").read_text(encoding="utf-8") + log_file.read_text(encoding="utf-8")
    assert all(f"request {number}\n" in lines for number in range(30))
    assert "after it was let go" in log_file.read_text(encoding="utf-8")


def test_serve_logs_a_logged_exception_by_its_type_alone(capsys, settings: Path, tmp_path: Path) -> None:
    log_file = tmp_path / "agent-bridge.log"

    def serve(app, **options) -> None:
        try:
            raise ValueError(f"a message with {TOKEN} in it")
        except ValueError as error:
            # As uvicorn logs an error in a request.
            logging.getLogger("uvicorn.error").error("Exception in ASGI application\n", exc_info=error)

    assert run(capsys, "serve", "--log-file", str(log_file), run=serve)[0] == 0
    text = log_file.read_text(encoding="utf-8")
    assert "ERROR     Exception in ASGI application\nValueError (traceback left out)\n" in text
    assert TOKEN not in text
    assert "Traceback" not in text


def test_serve_logs_why_it_wont_start(capsys, settings: Path, tmp_path: Path) -> None:
    settings.write_text(f"AGENT_BRIDGE_TOKEN={TOKEN}\nAGENT_BRIDGE_ALLOWED_ROOTS={settings.parent}\n")
    log_file = tmp_path / "agent-bridge.log"

    code, _, err = run(capsys, "serve", "--log-file", str(log_file), run=lambda app, **options: pytest.fail("served"))

    assert code == 1
    text = log_file.read_text(encoding="utf-8")
    assert "ERROR     Not starting: Set AGENT_BRIDGE_HOST, AGENT_BRIDGE_NAME, AGENT_BRIDGE_HERDR_BIN" in text
    assert "AGENT_BRIDGE_HOST" in err


def test_serve_logs_only_the_type_of_an_error_it_stops_on(capsys, settings: Path, tmp_path: Path) -> None:
    log_file = tmp_path / "agent-bridge.log"

    def serve(app, **options) -> None:
        raise ValueError(f"a message with {TOKEN} in it")

    with pytest.raises(ValueError):
        cli.main(["serve", "--log-file", str(log_file)], run=serve)

    text = log_file.read_text(encoding="utf-8")
    assert "ERROR     agent-bridge stopped: ValueError" in text
    assert TOKEN not in text


def test_serve_refuses_a_log_file_it_cant_write(capsys, settings: Path, tmp_path: Path) -> None:
    # A folder where the file should be.
    log_file = tmp_path / "agent-bridge.log"
    log_file.mkdir()

    code, _, err = run(capsys, "serve", "--log-file", str(log_file), run=lambda app, **options: pytest.fail("served"))

    assert code == 1
    assert f"Can't write {log_file}" in err


def test_enroll_sends_this_bridge_and_keeps_the_gateway(capsys, settings: Path, fake: FakeGateway) -> None:
    fake.answers[("POST", "enroll")] = httpx.Response(200, json={"name": "mac", "gateway_id": "g" * 32, "api_version": 2})

    code, out, _ = run(capsys, "enroll", STRING)

    assert code == 0
    assert "Enrolled as mac" in out
    state = load(state_path())
    assert fake.body() == {"secret": "a" * 24, "bridge_id": state.bridge_id, "url": "http://192.168.1.20:8765",
                           "token": TOKEN, "name": "mac"}
    assert str(fake.requests[-1].url) == f"{GATEWAY}/enroll"
    assert state.gateway == Gateway(url=GATEWAY, gateway_id="g" * 32, name="mac")


def test_enroll_keeps_the_bridge_id_it_serves_with(capsys, settings: Path, fake: FakeGateway) -> None:
    save(state_path(), State(bridge_id="1" * 32))
    fake.answers[("POST", "enroll")] = httpx.Response(200, json={"name": "mac", "gateway_id": "g" * 32, "api_version": 2})

    run(capsys, "enroll", STRING)

    assert fake.body()["bridge_id"] == "1" * 32


def test_enroll_exits_3_when_the_name_is_taken(capsys, settings: Path, fake: FakeGateway) -> None:
    fake.answers[("POST", "enroll")] = httpx.Response(
        409, json={"error": {"code": "name_taken", "message": "There is already a host named mac."}}
    )

    code, _, err = run(capsys, "enroll", STRING)

    assert code == 3
    assert "already a host named mac" in err
    assert load(state_path()).gateway is None


def test_enroll_exits_4_with_the_retry_command_when_unreachable(capsys, settings: Path, fake: FakeGateway) -> None:
    code, _, err = run(capsys, "enroll", STRING)

    assert code == 4
    assert f"agent-bridge enroll '{STRING}'" in err


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(401, json={"error": {"code": "wrong_secret", "message": "That isn't the current string."}}),
        httpx.Response(500, text="oops"),
    ],
)
def test_enroll_exits_1_on_other_refusals(capsys, settings: Path, fake: FakeGateway, answer) -> None:
    fake.answers[("POST", "enroll")] = answer

    code, _, err = run(capsys, "enroll", STRING)

    assert code == 1
    assert "Couldn't enroll" in err


@pytest.mark.parametrize("answer", [httpx.Response(200, json={"ok": True}), httpx.Response(200, text="<html>")])
def test_enroll_exits_1_when_another_server_answers(capsys, settings: Path, fake: FakeGateway, answer) -> None:
    fake.answers[("POST", "enroll")] = answer

    code, _, err = run(capsys, "enroll", STRING)

    assert code == 1
    assert "not as an mcp-gateway" in err
    assert load(state_path()).gateway is None


def test_enroll_warns_when_the_gateway_is_older(capsys, settings: Path, fake: FakeGateway) -> None:
    fake.answers[("POST", "enroll")] = httpx.Response(200, json={"name": "mac", "gateway_id": "g" * 32, "api_version": 1})

    code, _, err = run(capsys, "enroll", STRING)

    assert code == 0
    assert "The gateway is older" in err


def test_enroll_exits_5_with_how_to_retry_when_the_state_cant_be_saved(
    capsys, settings: Path, fake: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.answers[("POST", "enroll")] = httpx.Response(200, json={"name": "mac", "gateway_id": "g" * 32, "api_version": 2})

    def full(path: Path, state: State) -> None:
        if state.gateway is not None:
            raise cli.StateError(f"Can't write {path} (No space left on device).")
        save(path, state)

    monkeypatch.setattr(cli, "save", full)

    code, _, err = run(capsys, "enroll", STRING)

    assert code == cli.SAVE_FAILED == 5
    assert "enrolled this host as mac, but Can't write" in err
    assert f"agent-bridge enroll '{STRING}'" in err


@pytest.mark.parametrize("string", ["crm1@192.168.1.10:8766", "http://192.168.1.10:8766", "crm1@host:8766#xyz"])
def test_enroll_refuses_a_bad_string(capsys, settings: Path, string: str) -> None:
    code, _, err = run(capsys, "enroll", string)

    assert code == 1
    assert "sudo mcp-gateway enroll" in err


def test_enroll_refuses_incomplete_settings(capsys, settings: Path) -> None:
    settings.write_text(f"AGENT_BRIDGE_TOKEN={TOKEN}\nAGENT_BRIDGE_ALLOWED_ROOTS={settings.parent}\n")

    assert run(capsys, "enroll", STRING)[0] == 1


def test_parses_an_ipv6_enrollment_string() -> None:
    assert gateway.parse_enrollment("crm1@[fd00::5]:8766#" + "b" * 24) == ("http://[fd00::5]:8766", "b" * 24)


def test_unenroll_removes_this_host_from_the_gateway(capsys, settings: Path, fake: FakeGateway) -> None:
    state = enrolled()
    fake.answers[("DELETE", "bridges")] = httpx.Response(204)

    code, out, _ = run(capsys, "unenroll")

    assert code == 0
    assert fake.requests[-1].url.path == f"/bridges/{state.bridge_id}"
    assert fake.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"
    assert load(state_path()) == State(bridge_id=state.bridge_id)
    assert "Left the gateway" in out


def test_unenroll_prints_the_gateway_command_when_unreachable(capsys, settings: Path, fake: FakeGateway) -> None:
    state = enrolled()

    code, _, err = run(capsys, "unenroll")

    assert code == 0
    assert "sudo mcp-gateway bridges remove mac" in err
    # Kept, so running it again can still leave.
    assert load(state_path()) == state


def test_unenroll_forgets_a_gateway_that_forgot_this_host(capsys, settings: Path, fake: FakeGateway) -> None:
    enrolled()
    fake.answers[("DELETE", "bridges")] = httpx.Response(404, json={"error": {"code": "not_enrolled", "message": "x"}})

    assert run(capsys, "unenroll")[0] == 0
    assert load(state_path()).gateway is None


def test_unenroll_keeps_the_gateway_on_any_other_404(capsys, settings: Path, fake: FakeGateway) -> None:
    # The gateway refusing a caller off the LAN, or another server now at its address.
    state = enrolled()
    fake.answers[("DELETE", "bridges")] = httpx.Response(404, json={"error": "not found"})

    code, _, err = run(capsys, "unenroll")

    assert code == 0
    assert "sudo mcp-gateway bridges remove mac" in err
    assert load(state_path()) == state


JOINED = {"gateway_id": "g", "api_version": 2}
# What a gateway from before PUT /bridges/{id}/name answers: Starlette's own 404.
NO_ROUTE = httpx.Response(404, text="Not Found")


def test_rename_renames_this_host_on_the_gateway(capsys, settings: Path, fake: FakeGateway) -> None:
    state = enrolled()
    fake.answers[("PUT", "bridges")] = httpx.Response(200, json={"name": "studio", **JOINED})

    code, out, _ = run(capsys, "rename", "studio")

    assert code == 0
    assert fake.requests[-1].url.path == f"/bridges/{state.bridge_id}/name"
    assert fake.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"
    assert fake.body() == {"name": "studio"}
    assert load(state_path()).gateway.name == "studio"
    assert "as studio" in out


def test_rename_exits_3_when_the_name_is_taken(capsys, settings: Path, fake: FakeGateway) -> None:
    state = enrolled()
    fake.answers[("PUT", "bridges")] = httpx.Response(
        409, json={"error": {"code": "name_taken", "message": "There is already a host named studio."}}
    )

    code, _, err = run(capsys, "rename", "studio")

    assert code == 3
    assert "already a host named studio" in err
    assert load(state_path()) == state


def test_rename_exits_4_with_the_gateway_command_when_unreachable(capsys, settings: Path, fake: FakeGateway) -> None:
    state = enrolled()

    code, _, err = run(capsys, "rename", "studio")

    assert code == 4
    assert "sudo mcp-gateway bridges rename mac studio" in err
    assert load(state_path()) == state


def test_rename_exits_6_with_the_gateway_command_when_the_gateway_is_too_old(
    capsys, settings: Path, fake: FakeGateway
) -> None:
    state = enrolled()
    # No such route, but the address report every gateway has answers.
    fake.answers[("PUT", "bridges")] = [NO_ROUTE, httpx.Response(200, json={"name": "mac", **JOINED})]

    code, _, err = run(capsys, "rename", "studio")

    assert code == cli.OUTDATED == 6
    assert fake.requests[-1].url.path == f"/bridges/{state.bridge_id}/address"
    assert "update the gateway" in err
    assert "sudo mcp-gateway bridges rename mac studio" in err
    assert load(state_path()) == state


def test_rename_names_the_gateways_current_name_in_its_command(capsys, settings: Path, fake: FakeGateway) -> None:
    # Renamed there with `bridges rename mac studio` since the bridge last asked.
    enrolled()
    fake.answers[("PUT", "bridges")] = [NO_ROUTE, httpx.Response(200, json={"name": "studio", **JOINED})]

    code, _, err = run(capsys, "rename", "office")

    assert code == cli.OUTDATED
    assert "sudo mcp-gateway bridges rename studio office" in err


def test_rename_exits_6_too_when_the_gateway_cant_reach_a_new_address(capsys, settings: Path, fake: FakeGateway) -> None:
    # E.g. Windows setup renaming while agent-bridge is stopped, with a new address in the env
    # file: the gateway knows this host's token, but can't ask the new address about itself.
    enrolled()
    unreachable = httpx.Response(502, json={"error": {"code": "bridge_unreachable", "message": "x"}})
    fake.answers[("PUT", "bridges")] = [NO_ROUTE, unreachable]

    code, _, err = run(capsys, "rename", "studio")

    assert code == cli.OUTDATED
    assert "sudo mcp-gateway bridges rename mac studio" in err


@pytest.mark.parametrize(("status", "code"), [(404, "not_enrolled"), (401, "unauthorized")])
def test_rename_says_to_enroll_again_when_the_gateway_doesnt_have_this_host(
    capsys, settings: Path, fake: FakeGateway, status: int, code: str
) -> None:
    state = enrolled()
    fake.answers[("PUT", "bridges")] = httpx.Response(status, json={"error": {"code": code, "message": "x"}})

    result, _, err = run(capsys, "rename", "studio")

    assert result == 1
    # It has no host named mac to rename.
    assert "bridges rename" not in err
    assert "sudo mcp-gateway enroll" in err
    assert load(state_path()) == state


def test_rename_says_to_enroll_again_when_a_gateway_too_old_doesnt_have_this_host(
    capsys, settings: Path, fake: FakeGateway
) -> None:
    # No rename route, and the address report says it has no such host (`bridges remove`d).
    enrolled()
    gone = httpx.Response(404, json={"error": {"code": "not_enrolled", "message": "x"}})
    fake.answers[("PUT", "bridges")] = [NO_ROUTE, gone]

    code, _, err = run(capsys, "rename", "studio")

    assert code == 1
    assert "bridges rename" not in err
    assert "sudo mcp-gateway enroll" in err


def test_rename_exits_5_when_the_gateway_renamed_it_but_the_state_cant_be_saved(
    capsys, settings: Path, fake: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = enrolled()
    fake.answers[("PUT", "bridges")] = httpx.Response(200, json={"name": "studio", **JOINED})

    def full(path: Path, gateway: Gateway, name: str) -> None:
        raise cli.StateError(f"Can't write {path} (No space left on device).")

    monkeypatch.setattr(cli, "rename", full)

    code, _, err = run(capsys, "rename", "studio")

    assert code == cli.SAVE_FAILED == 5
    assert "has this host as studio now, but Can't write" in err
    assert "agent-bridge check" in err
    # The gateway has no host named mac any more.
    assert "bridges rename" not in err
    assert load(state_path()) == state


def test_rename_exits_1_on_any_other_404(capsys, settings: Path, fake: FakeGateway) -> None:
    # E.g. the gateway refusing a caller off the LAN: the address report is refused too.
    state = enrolled()
    off_lan = httpx.Response(404, json={"error": "not found"})
    fake.answers[("PUT", "bridges")] = [off_lan, off_lan]

    code, _, err = run(capsys, "rename", "studio")

    assert code == 1
    assert "sudo mcp-gateway bridges rename mac studio" in err
    assert load(state_path()) == state


def test_rename_exits_1_when_not_enrolled(capsys, settings: Path, fake: FakeGateway) -> None:
    code, _, err = run(capsys, "rename", "studio")

    assert code == 1
    assert "Not enrolled" in err
    assert fake.requests == []


def test_rename_exits_2_for_a_name_that_wont_do(capsys, settings: Path, fake: FakeGateway) -> None:
    enrolled()

    code, _, err = run(capsys, "rename", "Studio Mac")

    assert code == 2
    assert "lowercase letter" in err
    assert fake.requests == []


def test_unenroll_without_a_gateway(capsys) -> None:
    code, out, _ = run(capsys, "unenroll")

    assert (code, out.strip()) == (0, "Not enrolled with a gateway.")


class FakeHerdr:
    def __init__(self, binary: str, fails: bool = False):
        self.fails = fails

    async def version(self) -> str:
        if self.fails:
            raise cli.HerdrError("cli_failed", "herdr not found")
        return "0.9.5"


def test_check_passes_with_the_service_up_and_enrolled(
    capsys, settings: Path, fake: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = enrolled()
    monkeypatch.setattr(cli, "Herdr", FakeHerdr)
    monkeypatch.setattr(cli.httpx, "get", lambda url, **kwargs: httpx.Response(
        200, json={"bridge_id": state.bridge_id}, request=httpx.Request("GET", url)))
    fake.answers[("PUT", "bridges")] = httpx.Response(200, json={"name": "mac", "gateway_id": "g", "api_version": 2})

    code, out, _ = run(capsys, "check")

    assert code == 0, out
    assert [line.split()[0] for line in out.splitlines()] == ["ok"] * 4
    assert fake.body() == {"url": "http://192.168.1.20:8765"}


def test_check_keeps_the_name_the_gateway_has_now(
    capsys, settings: Path, fake: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Renamed there with `sudo mcp-gateway bridges rename mac studio`.
    state = enrolled()
    monkeypatch.setattr(cli, "Herdr", FakeHerdr)
    monkeypatch.setattr(cli.httpx, "get", lambda url, **kwargs: httpx.Response(
        200, json={"bridge_id": state.bridge_id}, request=httpx.Request("GET", url)))
    fake.answers[("PUT", "bridges")] = httpx.Response(200, json={"name": "studio", "gateway_id": "g", "api_version": 2})

    code, out, _ = run(capsys, "check")

    assert code == 0, out
    assert "has this host as studio" in out
    assert load(state_path()).gateway.name == "studio"


@pytest.mark.parametrize(("status", "code"), [(404, "not_enrolled"), (401, "unauthorized")])
def test_check_says_to_enroll_again_when_the_gateway_doesnt_have_this_host(
    capsys, settings: Path, fake: FakeGateway, monkeypatch: pytest.MonkeyPatch, status: int, code: str
) -> None:
    # E.g. after the gateway was reinstalled.
    state = enrolled()
    monkeypatch.setattr(cli, "Herdr", FakeHerdr)
    monkeypatch.setattr(cli.httpx, "get", lambda url, **kwargs: httpx.Response(
        200, json={"bridge_id": state.bridge_id}, request=httpx.Request("GET", url)))
    fake.answers[("PUT", "bridges")] = httpx.Response(status, json={"error": {"code": code, "message": "Gone."}})

    result, out, _ = run(capsys, "check")

    assert result == 1
    assert out.splitlines()[-1] == f"FAIL  the gateway at {state.gateway.url} has this host (Gone. {cli.ENROLL_AGAIN})"


def test_check_fails_with_one_line_per_check(
    capsys, settings: Path, fake: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    enrolled()
    if os.name == "posix":
        settings.chmod(0o644)
    monkeypatch.setattr(cli, "Herdr", lambda binary: FakeHerdr(binary, fails=True))

    def refused(url: str, **kwargs) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(cli.httpx, "get", refused)

    code, out, _ = run(capsys, "check")

    assert code == 1
    lines = out.splitlines()
    assert lines[0].startswith("ok    settings")
    assert all(line.startswith("FAIL") for line in lines[1:])
    assert any("herdr" in line for line in lines)
    assert any("agent-bridge answers" in line for line in lines)
    assert any("the gateway at" in line for line in lines)
    assert any("chmod 600" in line for line in lines) == (os.name == "posix")


def test_check_without_settings_fails_in_one_line(capsys) -> None:
    code, out, _ = run(capsys, "check")

    assert code == 1
    assert out.startswith("FAIL  settings") and len(out.splitlines()) == 1


def test_the_address_is_reported_on_start(fake: FakeGateway, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setattr(gateway, "REPORT_DELAYS", (0, 0, 0))
    state = State(bridge_id="1" * 32, gateway=Gateway(url=GATEWAY, gateway_id="g", name="mac"))
    settings = Settings(token=TOKEN, allowed_roots=(Path.cwd().resolve(),), host="192.168.1.20", port=9000)
    caplog.set_level(logging.INFO)
    # Not listening yet when the gateway checks, then up.
    fake.answers[("PUT", "bridges")] = [
        httpx.Response(502, json={"error": {"code": "bridge_unreachable", "message": "x"}}),
        httpx.Response(200, json={"name": "mac", "gateway_id": "g", "api_version": 2}),
    ]

    asyncio.run(gateway.keep_address(state, settings))

    assert len(fake.requests) == 2
    assert json.loads(fake.requests[-1].content) == {"url": "http://192.168.1.20:9000"}
    assert "has this host as mac" in caplog.text


def test_the_address_report_keeps_the_name_the_gateway_has_now(fake: FakeGateway, monkeypatch) -> None:
    monkeypatch.setattr(gateway, "REPORT_DELAYS", (0,))
    state = enrolled()
    settings = Settings(token=TOKEN, allowed_roots=(Path.cwd().resolve(),), host="192.168.1.20")
    fake.answers[("PUT", "bridges")] = httpx.Response(200, json={"name": "studio", "gateway_id": "g", "api_version": 2})

    asyncio.run(gateway.keep_address(state, settings, state_path()))

    assert load(state_path()).gateway.name == "studio"
    # Not over a state file enrolled with another gateway since.
    save(state_path(), State(bridge_id=state.bridge_id))
    asyncio.run(gateway.keep_address(state, settings, state_path()))
    assert load(state_path()).gateway is None


def test_a_failed_address_report_says_how_to_enroll_again(fake: FakeGateway, monkeypatch, caplog) -> None:
    monkeypatch.setattr(gateway, "REPORT_DELAYS", (0, 0))
    state = State(bridge_id="1" * 32, gateway=Gateway(url=GATEWAY, gateway_id="g", name="mac"))
    settings = Settings(token=TOKEN, allowed_roots=(Path.cwd().resolve(),), host="192.168.1.20")

    asyncio.run(gateway.keep_address(state, settings))

    assert len(fake.requests) == 2
    assert "agent-bridge enroll <string>" in caplog.text


@pytest.mark.parametrize(("status", "code"), [(404, "not_enrolled"), (401, "unauthorized")])
def test_the_address_report_stops_when_retrying_cant_help(
    fake: FakeGateway, monkeypatch, caplog, status: int, code: str
) -> None:
    monkeypatch.setattr(gateway, "REPORT_DELAYS", (0, 0, 0))
    state = State(bridge_id="1" * 32, gateway=Gateway(url=GATEWAY, gateway_id="g", name="mac"))
    settings = Settings(token=TOKEN, allowed_roots=(Path.cwd().resolve(),), host="192.168.1.20")
    fake.answers[("PUT", "bridges")] = httpx.Response(status, json={"error": {"code": code, "message": "x"}})

    asyncio.run(gateway.keep_address(state, settings))

    assert len(fake.requests) == 1
    assert "agent-bridge enroll <string>" in caplog.text
