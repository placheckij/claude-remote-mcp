"""The gateway with several agent hosts: ids, list_agents, start_agent, versions, and the
registry changing while it runs."""

import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from mcp_gateway import tools
from mcp_gateway.bridge import AgentBridge, BridgeError
from mcp_gateway.config import Settings
from mcp_gateway.hosts import Hosts
from mcp_gateway.registry import Registry
from mcp_gateway.server import create_app

# Test modules can't import each other under the root run's importlib mode, so these
# repeat test_server.py's.
MCP_TOKEN = "g" * 32
HOST = "mcp.example.com"
SETTINGS = Settings(
    token=MCP_TOKEN, oauth_passphrase="p" * 24, state_dir=Path("/nonexistent"), public_host=HOST,
    projects_root="/home/me/projects",
)
AGENT = {
    "id": "w1:p1", "name": "grafik", "title": "repo-1", "status": "idle", "cwd": "/home/me/projects/grafik",
    "workspace_id": "w1", "project": "grafik",
}
INFO = {
    "api_version": 2, "name": "mac", "platform": "darwin", "bridge_version": "0.1.0", "herdr_version": "0.9.5",
    "roots": ["/Users/me/projects"], "projects_root": "/Users/me/projects", "accounts": [], "max_agents": 10,
}
NO_INFO = httpx.Response(404, json={"detail": "Not Found"})
TOKEN = "b" * 32


class FakeAgentBridge:
    """Stands in for agent-bridge; records requests and answers from a route table."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        self.requests.append(request)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        response = self.routes[(request.method, request.url.path)]
        if isinstance(response, Exception):
            raise response
        return response(request) if callable(response) else response


def rpc(client: TestClient, method: str, params: dict) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {MCP_TOKEN}", "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json", "Host": HOST,
    }
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


def call_tool(client: TestClient, name: str, **arguments) -> tuple[bool, str]:
    response = rpc(client, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return result.get("isError", False), result["content"][0]["text"]


def info(name: str, api_version: int = 2) -> dict:
    return {**INFO, "name": name, "api_version": api_version}


def url(name: str) -> str:
    return f"http://{name}.lan:8765"


class Gateway:
    """The gateway with a fake agent-bridge per host, added to its registry in order."""

    def __init__(self, tmp_path: Path, names: list[str], **settings):
        self.state_dir = tmp_path
        self.fakes = {name: FakeAgentBridge({("GET", "/info"): httpx.Response(200, json=info(name))}) for name in names}
        self.registry = Registry(tmp_path / "bridges.json")
        for name in names:
            self.registry.add(name, url(name), TOKEN, info(name))
        self.connected: list[str] = []
        settings = SETTINGS.model_copy(update={"state_dir": tmp_path, **settings})
        self.client = TestClient(create_app(settings, self.connect), base_url=f"https://{HOST}")

    def connect(self, bridge_url: str, token: str) -> AgentBridge:
        self.connected.append(bridge_url)
        fake = self.fake(bridge_url.removeprefix("http://").split(".")[0])
        return AgentBridge(httpx.AsyncClient(base_url=bridge_url, transport=httpx.MockTransport(fake),
                                             headers={"Authorization": f"Bearer {token}"}))

    def fake(self, name: str) -> FakeAgentBridge:
        """The host's fake agent-bridge, also for a host added while the gateway runs."""
        return self.fakes.setdefault(name, FakeAgentBridge({("GET", "/info"): httpx.Response(200, json=info(name))}))

    def route(self, name: str, method: str, path: str, response) -> None:
        self.fake(name).routes[(method, path)] = response

    def paths(self, name: str) -> list[str]:
        return [request.url.path for request in self.fakes[name].requests]

    def call(self, tool: str, **arguments) -> tuple[bool, str]:
        return call_tool(self.client, tool, **arguments)


@pytest.fixture
def make(tmp_path: Path):
    gateways = []

    def make(names: list[str], **settings) -> Gateway:
        gateway = Gateway(tmp_path, names, **settings)
        gateway.client.__enter__()
        gateways.append(gateway)
        return gateway

    yield make
    for gateway in gateways:
        gateway.client.__exit__(None, None, None)


def agents(*ids: str) -> httpx.Response:
    return httpx.Response(200, json=[{**AGENT, "id": agent_id} for agent_id in ids])


async def too_slow(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(5)
    return httpx.Response(200, json=[])


# --- list_agents ----------------------------------------------------------------------


def test_list_agents_groups_by_host_and_names_the_ones_that_failed(
    make, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "LIST_TIMEOUT_S", 0.5)
    gateway = make(["mac", "linux", "pc", "slow", "down", "broken"])
    gateway.route("mac", "GET", "/agents", agents("w1:p1", "w2:p1"))
    gateway.route("linux", "GET", "/agents", agents("w1:p1"))
    gateway.route("pc", "GET", "/agents", agents())
    gateway.route("slow", "GET", "/agents", too_slow)
    gateway.fakes["down"].down = True
    gateway.route("broken", "GET", "/agents", httpx.Response(500, json={"detail": "herdr crashed"}))
    assert gateway.call("list_agents") == (False, (
        "3 agents on 6 hosts.\n"
        "mac, 2 agents:\n"
        "mac/w1:p1 · grafik · idle · grafik\n"
        "mac/w2:p1 · grafik · idle · grafik\n"
        "linux, 1 agent:\n"
        "linux/w1:p1 · grafik · idle · grafik\n"
        "No agents on pc.\n"
        "slow didn't answer: It didn't answer within 0.5 s.\n"
        "down didn't answer: The agent host is not reachable (ConnectError). It may be asleep or offline.\n"
        "broken didn't answer: herdr crashed"
    ))


@pytest.mark.parametrize(
    ("api_version", "says"),
    [(0, "old needs an update: its agent-bridge is too old for this gateway."),
     (3, "old needs an update: its agent-bridge is newer than this gateway, so update the gateway.")],
)
def test_list_agents_names_a_host_out_of_range_and_doesnt_call_it(make, api_version: int, says: str) -> None:
    gateway = make(["mac", "old"])
    gateway.route("mac", "GET", "/agents", agents("w1:p1"))
    gateway.route("old", "GET", "/info", httpx.Response(200, json=info("old", api_version)))
    is_error, text = gateway.call("list_agents")
    assert not is_error and text.endswith(f"\n{says}")
    assert gateway.paths("old") == ["/info"]


def test_list_agents_with_one_host_out_of_range_says_it_needs_an_update(make) -> None:
    gateway = make(["mac"])
    gateway.route("mac", "GET", "/info", httpx.Response(200, json=info("mac", 3)))
    is_error, text = gateway.call("list_agents")
    assert is_error and text.endswith("mac needs an update: its agent-bridge is newer than this gateway, so update the gateway.")
    assert gateway.paths("mac") == ["/info"]


def test_list_agents_with_one_host_waits_past_the_several_host_limit(make, monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing else to list, so it waits as long as before there were several hosts.
    monkeypatch.setattr(tools, "LIST_TIMEOUT_S", 0.1)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return agents("w1:p1")

    gateway = make(["mac"])
    gateway.route("mac", "GET", "/agents", slow)
    assert gateway.call("list_agents") == (False, "1 agent:\nw1:p1 · grafik · idle · grafik")


def test_one_version_behind_is_still_called(make) -> None:
    # An agent-bridge from before /info is API 1, one behind this gateway's 2.
    gateway = make(["mac", "old"])
    gateway.route("mac", "GET", "/agents", agents())
    gateway.route("old", "GET", "/info", NO_INFO)
    gateway.route("old", "GET", "/agents", agents("w1:p1"))
    assert "old/w1:p1 · grafik" in gateway.call("list_agents")[1]


def test_list_agents_with_no_hosts_says_how_to_add_one(make) -> None:
    is_error, text = make([]).call("list_agents")
    assert is_error and text.endswith("Add one on the gateway host with `sudo mcp-gateway bridges add <url>`.")


# --- Agent ids ------------------------------------------------------------------------


def test_a_qualified_id_reaches_its_host_with_the_bare_id(make) -> None:
    gateway = make(["mac", "linux"])
    gateway.route("linux", "GET", "/agents/w1:p1", httpx.Response(200, json=AGENT))
    assert gateway.call("agent_details", agent_id="linux/w1:p1") == (False, "linux/w1:p1 · grafik · idle · grafik")
    assert gateway.paths("mac") == []


def test_a_bare_id_needs_its_host_when_there_are_several(make) -> None:
    gateway = make(["mac", "linux"])
    is_error, text = gateway.call("stop_agent", agent_id="w1:p1")
    assert is_error and text.endswith(
        'Several hosts are set up; give the agent id with its host, like "mac/w1:p1". Hosts: mac, linux.'
    )
    assert gateway.paths("mac") == gateway.paths("linux") == []


def test_an_unknown_host_lists_the_hosts(make) -> None:
    is_error, text = make(["mac", "linux"]).call("stop_agent", agent_id="pi/w1:p1")
    assert is_error and text.endswith("No host named pi; hosts: mac, linux.")


def test_with_one_host_bare_and_qualified_ids_both_work(make) -> None:
    gateway = make(["mac"])
    gateway.route("mac", "DELETE", "/agents/w1:p1", httpx.Response(204))
    assert gateway.call("stop_agent", agent_id="w1:p1") == (False, "Stopped w1:p1.")
    assert gateway.call("stop_agent", agent_id="mac/w1:p1") == (False, "Stopped mac/w1:p1.")
    assert gateway.paths("mac") == ["/agents/w1:p1", "/agents/w1:p1"]


@pytest.mark.parametrize("agent_id", ["Mac/w1:p1", "mac/linux/w1:p1", "/w1:p1", "mac/", "1mac/w1:p1"])
def test_refuses_ids_with_a_malformed_host(make, agent_id: str) -> None:
    is_error, text = make(["mac", "linux"]).call("stop_agent", agent_id=agent_id)
    assert is_error and text.endswith('agent_id must be an id from list_agents, like "w1:p1".')


def test_replies_qualify_the_agent_id(make) -> None:
    gateway = make(["mac", "linux"])
    gateway.route("linux", "POST", "/agents/w1:p1/wait", httpx.Response(
        200, json={"agent": AGENT, "output": {"text": "done", "truncated": False}, "timed_out": False}
    ))
    assert "\nlinux/w1:p1 · grafik · idle · grafik\n" in gateway.call("wait_for_agent", agent_id="linux/w1:p1")[1]


def test_a_host_out_of_range_is_refused_unless_it_was_updated(make) -> None:
    gateway = make(["mac", "old"])
    gateway.route("old", "GET", "/info", httpx.Response(200, json=info("old", 0)))
    gateway.call("list_agents")
    is_error, text = gateway.call("stop_agent", agent_id="old/w1:p1")
    assert is_error and text.endswith("old needs an update: its agent-bridge is too old for this gateway.")
    # Updated since: asked again before refusing.
    gateway.route("old", "GET", "/info", httpx.Response(200, json=info("old")))
    gateway.route("old", "DELETE", "/agents/w1:p1", httpx.Response(204))
    assert gateway.call("stop_agent", agent_id="old/w1:p1") == (False, "Stopped old/w1:p1.")


def test_an_outdated_host_that_is_down_says_so(make) -> None:
    gateway = make(["mac", "old"])
    gateway.route("old", "GET", "/info", httpx.Response(200, json=info("old", 0)))
    gateway.call("list_agents")
    gateway.fakes["old"].down = True
    is_error, text = gateway.call("stop_agent", agent_id="old/w1:p1")
    assert is_error and text.endswith("The agent host is not reachable (ConnectError). It may be asleep or offline.")


# --- start_agent ----------------------------------------------------------------------


def started(name: str = "grafik") -> httpx.Response:
    return httpx.Response(201, json={**AGENT, "id": "w9:p1", "name": name})


def project(cwd: str | None) -> httpx.Response:
    if cwd is None:
        return httpx.Response(404, json={"error": {"code": "project_not_found", "message": "no"}})
    return httpx.Response(200, json={"cwd": cwd})


def test_start_agent_starts_on_the_one_host_that_has_the_project(make) -> None:
    gateway = make(["mac", "linux", "down"])
    gateway.route("mac", "GET", "/projects/grafik", project(None))
    gateway.route("linux", "GET", "/projects/grafik", project("/home/me/grafik"))
    gateway.route("linux", "POST", "/agents", started())
    gateway.fakes["down"].down = True
    # Starts on the one host found, and says which it couldn't ask: it may have grafik too.
    assert gateway.call("start_agent", project="grafik") == (
        False, "Started linux/w9:p1 · grafik · idle · grafik\nCouldn't ask down, which may have grafik too."
    )
    assert "/agents" not in gateway.paths("mac")


def test_start_agent_asks_which_host_when_several_have_the_project(make) -> None:
    gateway = make(["mac", "linux", "pc"])
    for name in ("mac", "linux", "pc"):
        gateway.route(name, "GET", "/projects/grafik", project("/somewhere/grafik"))
    assert gateway.call("start_agent", project="grafik") == (
        False, "grafik is on mac, linux and pc; which one? Start it again with that host."
    )
    assert not any("/agents" in gateway.paths(name) for name in ("mac", "linux", "pc"))


def test_start_agent_says_when_no_host_has_the_project(make) -> None:
    gateway = make(["mac", "linux", "down"])
    gateway.route("mac", "GET", "/projects/grafik", project(None))
    gateway.route("linux", "GET", "/projects/grafik", project(None))
    gateway.fakes["down"].down = True
    is_error, text = gateway.call("start_agent", project="grafik")
    assert is_error and text.endswith("No host has a folder grafik. Couldn't ask down.")


@pytest.mark.parametrize("api_version", [0, 3])
def test_start_agent_refuses_a_host_out_of_range(make, api_version: int) -> None:
    gateway = make(["mac", "old"])
    gateway.route("old", "GET", "/info", httpx.Response(200, json=info("old", api_version)))
    is_error, text = gateway.call("start_agent", project="grafik", host="old")
    assert is_error and "old needs an update" in text
    assert gateway.paths("old") == ["/info"]


def test_start_agent_names_the_hosts_it_couldnt_ask_for_the_project(make) -> None:
    # One needs an update; one is from before /info, so it can't look project names up.
    gateway = make(["mac", "new", "old"])
    gateway.route("mac", "GET", "/projects/grafik", project(None))
    gateway.route("new", "GET", "/info", httpx.Response(200, json=info("new", 3)))
    gateway.route("old", "GET", "/info", NO_INFO)
    is_error, text = gateway.call("start_agent", project="grafik")
    assert is_error and text.endswith("No host has a folder grafik. Couldn't ask new and old.")
    assert gateway.paths("new") == gateway.paths("old") == ["/info"]


def test_start_agent_on_a_named_host_asks_no_other(make) -> None:
    gateway = make(["mac", "linux"])
    gateway.route("mac", "POST", "/agents", started())
    assert gateway.call("start_agent", project="grafik", host="mac") == (False, "Started mac/w9:p1 · grafik · idle · grafik")
    assert gateway.paths("linux") == []
    is_error, text = gateway.call("start_agent", project="grafik", host="pi")
    assert is_error and text.endswith("No host named pi; hosts: mac, linux.")


@pytest.mark.parametrize("path", ["/srv/grafik", "C:\\work\\grafik", "\\\\server\\share\\grafik"])
def test_an_absolute_path_needs_a_host_when_there_are_several(make, path: str) -> None:
    gateway = make(["mac", "linux"])
    is_error, text = gateway.call("start_agent", project=path)
    assert is_error and text.endswith(f"Say which host has {path}: hosts mac and linux.")
    gateway.route("linux", "POST", "/agents", started())
    assert not gateway.call("start_agent", project=path, host="linux")[0]


def test_an_absolute_path_needs_no_host_when_there_is_one(make) -> None:
    gateway = make(["mac"])
    gateway.route("mac", "POST", "/agents", started())
    assert gateway.call("start_agent", project="/srv/grafik") == (False, "Started w9:p1 · grafik · idle · grafik")


def test_project_names_are_sent_as_one_path_segment(make) -> None:
    gateway = make(["mac", "linux"])
    gateway.route("mac", "GET", "/projects/a/b", project(None))
    gateway.route("linux", "GET", "/projects/a/b", project(None))
    gateway.call("start_agent", project="a/b")
    (request,) = [r for r in gateway.fakes["mac"].requests if r.url.path.startswith("/projects")]
    assert request.url.raw_path == b"/projects/a%2Fb"


# --- The registry while the gateway runs ----------------------------------------------


def test_takes_in_hosts_added_renamed_and_removed_while_running(make) -> None:
    gateway = make(["mac"])
    gateway.route("mac", "DELETE", "/agents/w1:p1", httpx.Response(204))
    cli = Registry(gateway.state_dir / "bridges.json")
    cli.add("linux", url("linux"), TOKEN, info("linux"))
    gateway.route("linux", "DELETE", "/agents/w1:p1", httpx.Response(204))
    assert gateway.call("stop_agent", agent_id="linux/w1:p1") == (False, "Stopped linux/w1:p1.")
    cli.rename("linux", "box")
    assert gateway.call("stop_agent", agent_id="box/w1:p1") == (False, "Stopped box/w1:p1.")
    # A rename keeps its client; only a new URL or token makes a new one.
    assert gateway.connected == [url("mac"), url("linux")]
    cli.remove("box")
    assert gateway.call("stop_agent", agent_id="w1:p1") == (False, "Stopped w1:p1.")


def test_the_instructions_name_the_hosts(make) -> None:
    gateway = make(["mac"])
    initialize = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}

    def instructions() -> str:
        return rpc(gateway.client, "initialize", initialize).json()["result"]["instructions"]

    assert instructions().endswith("Agent hosts: mac.\n")
    Registry(gateway.state_dir / "bridges.json").remove("mac")
    assert instructions().endswith("No agent hosts are set up yet.\n")
    Registry(gateway.state_dir / "bridges.json").add("mac", url("mac"), TOKEN, info("mac"))
    Registry(gateway.state_dir / "bridges.json").add("linux", url("linux"), TOKEN, info("linux"))
    assert instructions().endswith("Agent hosts: mac, linux.\n")


def test_a_broken_registry_file_stops_startup(tmp_path: Path) -> None:
    (tmp_path / "bridges.json").write_text("{")
    with pytest.raises(RuntimeError, match="Can't read the agent hosts"):
        create_app(SETTINGS.model_copy(update={"state_dir": tmp_path}))


# --- Legacy import --------------------------------------------------------------------


def legacy_gateway(tmp_path: Path, fake: FakeAgentBridge) -> TestClient:
    settings = SETTINGS.model_copy(update={"state_dir": tmp_path, "bridge_url": url("mac"), "bridge_token": TOKEN})

    def connect(bridge_url: str, token: str) -> AgentBridge:
        return AgentBridge(httpx.AsyncClient(base_url=bridge_url, transport=httpx.MockTransport(fake),
                                             headers={"Authorization": f"Bearer {token}"}))

    return TestClient(create_app(settings, connect), base_url=f"https://{HOST}")


def saved(tmp_path: Path) -> list[dict]:
    return json.loads((tmp_path / "bridges.json").read_text())["bridges"]


def test_the_first_start_imports_the_legacy_bridge_by_its_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeAgentBridge({("GET", "/info"): httpx.Response(200, json=INFO), ("GET", "/agents"): agents("w1:p1")})
    with caplog.at_level(logging.WARNING), legacy_gateway(tmp_path, fake) as client:
        # Imported at startup, before any request, so `bridges list` shows it at once.
        (bridge,) = saved(tmp_path)
        assert (bridge["name"], bridge["url"], bridge["token"], bridge["info"]) == ("mac", url("mac"), TOKEN, INFO)
        # Bare ids, as before.
        assert call_tool(client, "list_agents") == (False, "1 agent:\nw1:p1 · grafik · idle · grafik")
    assert "MCP_GATEWAY_BRIDGE_URL and MCP_GATEWAY_BRIDGE_TOKEN are no longer needed" in caplog.text


@pytest.mark.parametrize(("answer", "stored"), [(NO_INFO, {"api_version": 1}), (httpx.ConnectError("refused"), None)])
def test_a_legacy_bridge_that_cant_say_its_name_is_imported_as_host(tmp_path: Path, answer, stored) -> None:
    fake = FakeAgentBridge({("GET", "/info"): answer})
    with legacy_gateway(tmp_path, fake):
        (bridge,) = saved(tmp_path)
    assert (bridge["name"], bridge["info"]) == ("host", stored)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_a_legacy_bridge_that_refuses_isnt_imported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    # A wrong token would make every call fail; left in the settings, setup.sh reports it.
    fake = FakeAgentBridge({("GET", "/info"): httpx.Response(status, json={"detail": "Invalid token."})})
    with caplog.at_level(logging.ERROR), legacy_gateway(tmp_path, fake) as client:
        is_error, text = call_tool(client, "list_agents")
    assert not (tmp_path / "bridges.json").exists()
    assert is_error and text.endswith("Add one on the gateway host with `sudo mcp-gateway bridges add <url>`.")
    assert "refused the import (Invalid token.)" in caplog.text


def test_the_legacy_bridge_is_ignored_once_there_are_hosts(tmp_path: Path) -> None:
    Registry(tmp_path / "bridges.json").add("linux", url("linux"), TOKEN, info("linux"))
    fake = FakeAgentBridge({})
    with legacy_gateway(tmp_path, fake):
        assert [bridge["name"] for bridge in saved(tmp_path)] == ["linux"]
    assert fake.requests == []


def test_a_legacy_import_that_cant_be_saved_leaves_the_gateway_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Registry, "add", full_disk)
    fake = FakeAgentBridge({("GET", "/info"): httpx.Response(200, json=INFO)})
    with caplog.at_level(logging.ERROR), legacy_gateway(tmp_path, fake) as client:
        is_error, text = call_tool(client, "list_agents")
    assert is_error and text.endswith("Add one on the gateway host with `sudo mcp-gateway bridges add <url>`.")
    assert "Couldn't import MCP_GATEWAY_BRIDGE_URL" in caplog.text


def test_an_unexpected_failure_at_the_legacy_import_still_serves_mcp(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Raised during the lifespan startup, it would leave /mcp unserved; logged instead.
    def broken(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("unforeseen")

    fake = FakeAgentBridge({("GET", "/info"): broken})
    with caplog.at_level(logging.ERROR), legacy_gateway(tmp_path, fake) as client:
        is_error, text = call_tool(client, "list_agents")
    assert is_error and text.endswith("Add one on the gateway host with `sudo mcp-gateway bridges add <url>`.")
    assert "Couldn't import MCP_GATEWAY_BRIDGE_URL" in caplog.text


@pytest.mark.parametrize("answer", [{}, {"cwd": 5}, ["/srv/grafik"]])
def test_a_projects_answer_without_a_folder_is_a_host_failing(answer) -> None:
    fake = FakeAgentBridge({("GET", "/projects/grafik"): httpx.Response(200, json=answer)})
    bridge = AgentBridge(httpx.AsyncClient(base_url=url("mac"), transport=httpx.MockTransport(fake),
                                           headers={"Authorization": f"Bearer {TOKEN}"}))
    with pytest.raises(BridgeError, match="answered /projects without a folder"):
        asyncio.run(bridge.find_project("grafik"))


def test_the_legacy_bridge_stays_removed(tmp_path: Path) -> None:
    # Once imported, removing it empties the registry; a restart with the old settings
    # still set must not bring it back.
    registry = Registry(tmp_path / "bridges.json")
    registry.add("mac", url("mac"), TOKEN, INFO)
    registry.remove("mac")
    fake = FakeAgentBridge({})
    with legacy_gateway(tmp_path, fake):
        assert saved(tmp_path) == []
    assert fake.requests == []


# --- Clients --------------------------------------------------------------------------


def hosts_on(tmp_path: Path) -> tuple[Hosts, Registry, dict[str, AgentBridge]]:
    """Hosts over a registry with mac, and the clients it made by URL."""
    clients: dict[str, AgentBridge] = {}

    def connect(bridge_url: str, token: str) -> AgentBridge:
        clients[bridge_url] = AgentBridge(httpx.AsyncClient(base_url=bridge_url))
        return clients[bridge_url]

    registry = Registry(tmp_path / "bridges.json")
    registry.add("mac", url("mac"), TOKEN, info("mac"))
    return Hosts(registry, connect), registry, clients


def test_a_change_taken_in_by_the_gateways_own_save_gets_a_client(tmp_path: Path) -> None:
    # The CLI adds a host just before the gateway saves what mac said about itself; that
    # save starts from the file, so it takes linux in without a reload.
    hosts, registry, _ = hosts_on(tmp_path)
    Registry(tmp_path / "bridges.json").add("linux", url("linux"), TOKEN, info("linux"))
    registry.seen(registry.bridges[0].bridge_id, info("mac", 1))
    assert [host.name for host in hosts.all()] == ["mac", "linux"]


@pytest.mark.parametrize(("field", "value"), [("url", url("studio")), ("token", "n" * 32)])
def test_a_new_url_or_token_gets_a_new_client(tmp_path: Path, field: str, value: str) -> None:
    hosts, _, clients = hosts_on(tmp_path)
    old = hosts.all()[0].bridge
    path = tmp_path / "bridges.json"
    data = json.loads(path.read_text())
    data["bridges"][0][field] = value
    path.write_text(json.dumps(data))
    asyncio.run(hosts.refresh())
    (host,) = hosts.all()
    assert host.bridge is not old and (host.record.url, host.record.token)[field == "token"] == value
    assert str(host.bridge.client.base_url).rstrip("/") == host.record.url
    # The old one may still be in use by a request; closed at shutdown.
    assert not old.client.is_closed
    asyncio.run(hosts.aclose())
    assert old.client.is_closed and host.bridge.client.is_closed


def test_a_new_url_taken_in_by_the_gateways_own_save_gets_a_new_client(tmp_path: Path) -> None:
    # The edit arrives through seen()'s save rather than a reload; the host's record then
    # already shows the new URL when the clients are compared.
    hosts, registry, _ = hosts_on(tmp_path)
    (host,) = hosts.all()
    old = host.bridge
    path = tmp_path / "bridges.json"
    data = json.loads(path.read_text())
    data["bridges"][0]["url"] = url("studio")
    path.write_text(json.dumps(data))
    registry.seen(host.record.bridge_id, info("mac", 1))
    host.record = registry.bridges[0]
    (host,) = hosts.all()
    assert host.bridge is not old
    assert str(host.bridge.client.base_url).rstrip("/") == url("studio")


def test_a_removed_hosts_client_is_closed_only_at_shutdown(tmp_path: Path) -> None:
    # A request that looked it up before the removal may still be using it.
    hosts, _, clients = hosts_on(tmp_path)
    Registry(tmp_path / "bridges.json").remove("mac")
    asyncio.run(hosts.refresh())
    assert hosts.all() == [] and not clients[url("mac")].client.is_closed
    asyncio.run(hosts.aclose())
    assert clients[url("mac")].client.is_closed


def test_an_odd_answer_from_one_host_fails_that_host_only(make) -> None:
    gateway = make(["mac", "odd", "linux"])
    gateway.route("mac", "GET", "/agents", agents("w1:p1"))
    gateway.route("odd", "GET", "/agents", httpx.Response(200, json={"agents": []}))
    gateway.route("linux", "GET", "/agents", agents())
    is_error, text = gateway.call("list_agents")
    assert not is_error and "mac/w1:p1 · grafik" in text
    assert text.endswith("odd didn't answer: The agent host answered /agents with something other than a list of agents.")
    # The same for the project lookup: the host with an odd answer counts as not asked.
    gateway.route("mac", "GET", "/projects/grafik", httpx.Response(200, json={}))
    gateway.route("odd", "GET", "/projects/grafik", project(None))
    gateway.route("linux", "GET", "/projects/grafik", project("/home/me/grafik"))
    gateway.route("linux", "POST", "/agents", started())
    assert gateway.call("start_agent", project="grafik") == (
        False, "Started linux/w9:p1 · grafik · idle · grafik\nCouldn't ask mac, which may have grafik too."
    )


def test_an_agent_that_cant_be_described_fails_its_host_only(
    make, caplog: pytest.LogCaptureFixture
) -> None:
    # An answer nothing checks for, e.g. an agent without a status: logged whole as a bug.
    gateway = make(["mac", "odd"])
    gateway.route("mac", "GET", "/agents", agents("w1:p1"))
    gateway.route("odd", "GET", "/agents", httpx.Response(200, json=[{"id": "w1:p1"}]))
    with caplog.at_level(logging.ERROR):
        is_error, text = gateway.call("list_agents")
    assert not is_error and "mac/w1:p1 · grafik" in text
    assert text.endswith("odd didn't answer: Its answer couldn't be used (KeyError).")
    assert "Asking odd failed unexpectedly" in caplog.text


def test_an_info_without_api_version_or_json_is_a_host_failing(make) -> None:
    gateway = make(["mac", "odd", "garbled"])
    gateway.route("mac", "GET", "/agents", agents())
    gateway.route("odd", "GET", "/info", httpx.Response(200, json={"name": "odd"}))
    gateway.route("garbled", "GET", "/info", httpx.Response(200, text="<html>"))
    is_error, text = gateway.call("list_agents")
    assert not is_error and text.endswith(
        "odd didn't answer: The agent host answered /info without an API version.\n"
        "garbled didn't answer: The agent host answered with something other than JSON."
    )
