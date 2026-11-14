"""`agent-bridge serve | enroll | unenroll | check`, against a fake gateway: the exit codes
and files the platform setups rely on."""

import asyncio
import json
import logging
import os
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


def test_a_failed_address_report_says_how_to_enroll_again(fake: FakeGateway, monkeypatch, caplog) -> None:
    monkeypatch.setattr(gateway, "REPORT_DELAYS", (0, 0))
    state = State(bridge_id="1" * 32, gateway=Gateway(url=GATEWAY, gateway_id="g", name="mac"))
    settings = Settings(token=TOKEN, allowed_roots=(Path.cwd().resolve(),), host="192.168.1.20")

    asyncio.run(gateway.keep_address(state, settings))

    assert len(fake.requests) == 2
    assert "agent-bridge enroll <string>" in caplog.text
