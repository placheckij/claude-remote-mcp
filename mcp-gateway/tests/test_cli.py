import json
import os
from pathlib import Path

import httpx
import pytest

from mcp_gateway import cli
from mcp_gateway.bridge import AgentBridge
from mcp_gateway.registry import Registry

TOKEN = "b" * 32
INFO = {
    "api_version": 2, "name": "mac", "platform": "darwin", "bridge_version": "0.1.0", "herdr_version": "0.9.5",
    "roots": ["/Users/me/projects"], "projects_root": "/Users/me/projects", "accounts": [], "max_agents": 10,
}


class Bridges:
    """Runs `mcp-gateway bridges …` against a fake agent-bridge answering /info."""

    def __init__(self, tmp_path: Path, capsys: pytest.CaptureFixture):
        self.state_dir = tmp_path
        self.capsys = capsys
        self.info: httpx.Response | Exception = httpx.Response(200, json=INFO)
        self.seen: list[httpx.Request] = []
        self.token = TOKEN

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if isinstance(self.info, Exception):
            raise self.info
        return self.info

    def connect(self, url: str, token: str) -> AgentBridge:
        return AgentBridge(httpx.AsyncClient(base_url=url, transport=httpx.MockTransport(self.answer),
                                             headers={"Authorization": f"Bearer {token}"}))

    def __call__(self, *args: str) -> tuple[int, str, str]:
        code = cli.main(["--state-dir", str(self.state_dir), "bridges", *args],
                        ask_token=lambda prompt: self.token, connect=self.connect)
        out, err = self.capsys.readouterr()
        return code, out, err

    def saved(self) -> list[dict]:
        return json.loads((self.state_dir / "bridges.json").read_text())["bridges"]


@pytest.fixture
def bridges(tmp_path: Path, capsys: pytest.CaptureFixture) -> Bridges:
    return Bridges(tmp_path, capsys)


def test_add_takes_the_name_and_the_rest_from_info(bridges: Bridges) -> None:
    code, out, _ = bridges("add", "http://192.168.1.20:8765/")
    assert (code, out) == (0, "Added mac (darwin, agent-bridge 0.1.0) at http://192.168.1.20:8765.\n")
    (bridge,) = bridges.saved()
    assert (bridge["name"], bridge["url"], bridge["token"], bridge["info"]) == (
        "mac", "http://192.168.1.20:8765", TOKEN, INFO
    )
    (request,) = bridges.seen
    assert (request.url.path, request.headers["authorization"]) == ("/info", f"Bearer {TOKEN}")


def test_add_takes_another_name(bridges: Bridges) -> None:
    assert bridges("add", "http://mac:8765", "--name", "studio")[0] == 0
    assert bridges.saved()[0]["name"] == "studio"


@pytest.mark.parametrize(
    ("info", "says"),
    [(httpx.Response(404, json={"detail": "Not Found"}), "has no /info: update agent-bridge on it first"),
     (httpx.Response(401, json={"error": {"code": "unauthorized", "message": "Invalid token."}}), "Invalid token."),
     (httpx.ConnectError("refused"), "not reachable")],
)
def test_add_refuses_a_bridge_that_doesnt_answer_info(bridges: Bridges, info, says: str) -> None:
    bridges.info = info
    code, out, err = bridges("add", "http://mac:8765")
    assert code == 1 and out == "" and says in err
    assert not (bridges.state_dir / "bridges.json").exists()


@pytest.mark.parametrize("url", ["mac:8765", "ftp://mac", "http://", "http://mac:8765/api"])
def test_add_refuses_a_url_that_isnt_a_bridge_base(bridges: Bridges, url: str) -> None:
    code, _, err = bridges("add", url)
    assert code == 1 and "like http://192.168.1.20:8765" in err
    assert bridges.seen == []


def test_add_refuses_an_empty_token(bridges: Bridges) -> None:
    bridges.token = "  "
    code, _, err = bridges("add", "http://mac:8765")
    assert code == 1 and "token" in err and bridges.seen == []


def test_add_refuses_a_taken_name_and_a_bad_one(bridges: Bridges) -> None:
    bridges("add", "http://mac:8765")
    code, _, err = bridges("add", "http://other:8765")
    assert code == 1 and "There is already a host named mac." in err
    code, _, err = bridges("add", "http://other:8765", "--name", "My Mac")
    assert code == 1 and "can't name a host" in err


def test_add_warns_about_a_version_the_gateway_wont_call(bridges: Bridges) -> None:
    bridges.info = httpx.Response(200, json={**INFO, "api_version": 3})
    code, _, err = bridges("add", "http://mac:8765")
    assert code == 0 and "mac needs an update" in err


def test_list_shows_each_host_without_its_token(bridges: Bridges) -> None:
    assert bridges("list")[1] == "No agent hosts. Add one with: mcp-gateway bridges add <url>\n"
    bridges("add", "http://mac:8765")
    Registry(bridges.state_dir / "bridges.json").add("linux", "http://linux:8765", TOKEN, None)
    code, out, _ = bridges("list")
    lines = out.splitlines()
    assert code == 0 and len(lines) == 2 and TOKEN not in out
    assert lines[0].startswith("mac    http://mac:8765    darwin, API 2, last seen ")
    assert lines[1] == "linux  http://linux:8765  never answered"


def test_rename_and_remove(bridges: Bridges) -> None:
    bridges("add", "http://mac:8765")
    assert bridges("rename", "mac", "studio")[:2] == (0, "Renamed mac to studio.\n")
    code, _, err = bridges("remove", "mac")
    assert code == 1 and "No host named mac; hosts: studio." in err
    assert bridges("remove", "studio")[:2] == (0, "Removed studio.\n")
    assert bridges.saved() == []


def test_the_state_dir_comes_from_the_environment(
    bridges: Bridges, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_GATEWAY_STATE_DIR", str(tmp_path / "state"))
    assert cli.main(["bridges", "list"]) == 0
    monkeypatch.delenv("MCP_GATEWAY_STATE_DIR")
    assert cli.main(["bridges", "list"]) == 1


@pytest.fixture
def as_root(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Pretends to run as root; the switch to another user is recorded, not made."""
    switched: list[tuple] = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "setgroups", lambda groups: switched.append(("groups", groups)))
    monkeypatch.setattr(os, "setgid", lambda gid: switched.append(("gid", gid)))
    monkeypatch.setattr(os, "setuid", lambda uid: switched.append(("uid", uid)))
    return switched


@pytest.mark.skipif(os.name != "posix", reason="no users to switch to")
def test_run_as_root_it_becomes_the_state_folders_owner_first(bridges: Bridges, as_root: list[tuple]) -> None:
    # Root's files there would be unreadable for the gateway, and the folder's owner could
    # plant links for root to follow.
    owner = bridges.state_dir.stat()
    assert bridges("list")[0] == 0
    assert as_root == [("groups", []), ("gid", owner.st_gid), ("uid", owner.st_uid)]


@pytest.mark.skipif(os.name != "posix", reason="no users to switch to")
def test_run_as_root_it_needs_the_state_folder(bridges: Bridges, as_root: list[tuple]) -> None:
    bridges.state_dir = bridges.state_dir / "missing"
    code, _, err = bridges("list")
    assert code == 1 and "start mcp-gateway once to create it" in err
    assert as_root == [] and not bridges.state_dir.exists()


@pytest.mark.skipif(os.name != "posix", reason="no users to switch to")
def test_run_as_root_on_a_folder_of_root_it_stays_root(bridges: Bridges, as_root: list[tuple]) -> None:
    bridges.state_dir = Path("/")
    assert bridges("list")[0] == 0 and as_root == []


@pytest.mark.parametrize("name", ["3dprinter", "jakubs-macbook-pro-14-inch-2023-m3-max-edition", None])
def test_add_points_to_name_when_the_bridge_names_itself_unusably(bridges: Bridges, name) -> None:
    # agent-bridge names itself after its hostname, which may start with a digit or be long.
    bridges.info = httpx.Response(200, json={**INFO, "name": name})
    code, _, err = bridges("add", "http://mac:8765")
    assert code == 1 and "Give it one with --name, like --name mac." in err
    assert bridges("add", "http://mac:8765", "--name", "printer")[0] == 0
