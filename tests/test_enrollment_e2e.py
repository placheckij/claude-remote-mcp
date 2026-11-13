"""One gateway and two agent-bridges (with a fake herdr) on loopback, each a real server:
the bridges enroll with the commands the platform setups run, then Claude's tools reach
agents on both, one bridge moves to another port, and one leaves."""

import json
import os
import socket
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn

from agent_bridge import cli as bridge_cli
from agent_bridge import gateway as bridge_gateway
from agent_bridge.app import create_app as bridge_app
from agent_bridge.config import Settings as BridgeSettings
from agent_bridge.herdr import Herdr
from agent_bridge.state import load, load_or_create
from mcp_gateway import cli as gateway_cli
from mcp_gateway.config import Settings as GatewaySettings
from mcp_gateway.server import create_app as gateway_app

MCP_TOKEN = "g" * 32


def ok(result: dict) -> tuple[int, str, str]:
    return 0, json.dumps({"id": "cli", "result": result}), ""


class FakeHerdr:
    """herdr on one host: agents start in a new workspace and answer prompts at once."""

    def __init__(self, host: str):
        self.host = host
        self.agents: list[dict] = []
        self.created: dict[str, str] = {}

    async def __call__(self, argv: Sequence[str]) -> tuple[int, str, str]:
        args = list(argv[1:])
        if args == ["--version"]:
            return 0, "herdr 0.9.5\n", ""
        if args[:2] == ["agent", "list"]:
            return ok({"agents": self.agents})
        if args[:2] == ["workspace", "create"]:
            workspace = f"w{len(self.created) + 1}"
            self.created[workspace] = args[args.index("--cwd") + 1]
            return ok({"workspace": {"workspace_id": workspace}, "root_pane": {"pane_id": f"{workspace}:p1"}})
        if args[:2] == ["pane", "wait-output"]:
            # No trust dialog.
            return 1, "", json.dumps({"id": "cli", "error": {"code": "timeout", "message": "timeout"}})
        if args[:2] == ["agent", "start"]:
            pane = args[args.index("--pane") + 1]
            workspace = pane.split(":")[0]
            agent = {"agent": "claude", "agent_status": "idle", "cwd": self.created[workspace],
                     "foreground_cwd": self.created[workspace], "name": args[2], "pane_id": pane,
                     "terminal_title_stripped": args[2], "workspace_id": workspace}
            self.agents.append(agent)
            return ok({"agent": agent})
        if args[:2] in (["agent", "get"], ["agent", "prompt"]):
            agent = next(a for a in self.agents if a["pane_id"] == args[2])
            if args[1] == "prompt":
                agent["agent_status"] = "done"
            return ok({"agent": agent})
        if args[:2] == ["agent", "read"]:
            return 0, f"Hello from {self.host}.\n", ""
        raise AssertionError(f"unexpected herdr call: {args}")


class Served:
    """An ASGI app served by uvicorn on a loopback port, in a thread."""

    def __init__(self, sock: socket.socket, app, **options):
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning", **options))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "server didn't start"
            time.sleep(0.01)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)


def loopback_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock


class Bridge:
    """An agent host: its env and state files, its projects folder and its server."""

    def __init__(self, name: str, folder: Path, monkeypatch: pytest.MonkeyPatch):
        self.name = name
        self.monkeypatch = monkeypatch
        self.env_file = folder / "env"
        self.state_file = folder / "state.json"
        self.root = folder / "projects"
        (self.root / f"{name}-app").mkdir(parents=True)
        self.herdr = FakeHerdr(name)
        self.served: Served | None = None

    def use(self) -> None:
        """Point the bridge's commands at this host's files."""
        self.monkeypatch.setenv("AGENT_BRIDGE_ENV_FILE", str(self.env_file))
        self.monkeypatch.setenv("AGENT_BRIDGE_STATE_FILE", str(self.state_file))

    def start(self) -> None:
        sock = loopback_socket()
        self.env_file.write_text(
            f"AGENT_BRIDGE_TOKEN={self.name[0] * 32}\nAGENT_BRIDGE_ALLOWED_ROOTS={self.root}\n"
            f"AGENT_BRIDGE_HOST=127.0.0.1\nAGENT_BRIDGE_PORT={sock.getsockname()[1]}\n"
            f"AGENT_BRIDGE_NAME={self.name}\nAGENT_BRIDGE_HERDR_BIN=herdr\n"
        )
        self.use()
        settings = BridgeSettings.from_env()
        app = bridge_app(settings, Herdr(runner=self.herdr), load_or_create(self.state_file), self.state_file)
        self.served = Served(sock, app)

    def stop(self) -> None:
        if self.served:
            self.served.stop()


@dataclass
class Gateway:
    url: str
    state_dir: Path

    def hosts(self) -> list[dict]:
        return json.loads((self.state_dir / "bridges.json").read_text())["bridges"]

    def open_enrollment(self, capsys: pytest.CaptureFixture) -> str:
        """Runs `mcp-gateway enroll`; the string it prints."""
        port = self.url.rsplit(":", 1)[1]
        assert gateway_cli.main(["--state-dir", str(self.state_dir), "enroll", "--address", "127.0.0.1",
                                 "--port", port]) == 0
        return next(word.strip("'") for word in capsys.readouterr().out.split() if word.startswith("'crm1@"))


@pytest.fixture
def hosts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Gateway, Bridge, Bridge]]:
    for name in list(os.environ):
        if name.startswith(("AGENT_BRIDGE_", "MCP_GATEWAY_")):
            monkeypatch.delenv(name)
    # The address report tries soon after a start.
    monkeypatch.setattr(bridge_gateway, "REPORT_DELAYS", (0.1, 0.2, 0.4, 0.8))
    gateway = Gateway("", tmp_path / "gateway")
    sock = loopback_socket()
    settings = GatewaySettings(oauth_passphrase="p" * 24, state_dir=gateway.state_dir,
                               public_host="mcp.example.com", token=MCP_TOKEN)
    # As deploy/setup.sh starts it.
    served = Served(sock, gateway_app(settings), proxy_headers=False)
    gateway.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    bridges = Bridge("alpha", tmp_path / "alpha", monkeypatch), Bridge("beta", tmp_path / "beta", monkeypatch)
    for bridge in bridges:
        bridge.start()
    try:
        yield gateway, *bridges
    finally:
        for bridge in bridges:
            bridge.stop()
        served.stop()


def tool(gateway: Gateway, name: str, **arguments) -> str:
    response = httpx.post(
        f"{gateway.url}/mcp",
        headers={"Authorization": f"Bearer {MCP_TOKEN}", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result["content"][0]["text"]
    return result["content"][0]["text"]


def enroll(bridge: Bridge, string: str, capsys: pytest.CaptureFixture) -> tuple[int, str]:
    bridge.use()
    code = bridge_cli.main(["enroll", string])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_two_bridges_enroll_and_serve_through_one_gateway(
    hosts: tuple[Gateway, Bridge, Bridge], capsys: pytest.CaptureFixture
) -> None:
    gateway, alpha, beta = hosts
    string = gateway.open_enrollment(capsys)

    assert enroll(alpha, string, capsys) == (0, f"Enrolled as alpha with the gateway at {gateway.url}.\n")
    assert enroll(beta, string, capsys)[0] == 0
    assert [host["name"] for host in gateway.hosts()] == ["alpha", "beta"]

    # A taken name, e.g. picked when re-running a setup, is refused with the code the
    # setups ask again on.
    beta.env_file.write_text(beta.env_file.read_text().replace("AGENT_BRIDGE_NAME=beta", "AGENT_BRIDGE_NAME=alpha"))
    assert enroll(beta, string, capsys)[0] == 3
    assert [host["name"] for host in gateway.hosts()] == ["alpha", "beta"]
    beta.env_file.write_text(beta.env_file.read_text().replace("AGENT_BRIDGE_NAME=alpha", "AGENT_BRIDGE_NAME=beta"))

    # Each project is on one host, so no host has to be named.
    started = tool(gateway, "start_agent", project="alpha-app")
    assert "alpha/w1:p1" in started
    tool(gateway, "start_agent", project="beta-app")
    listed = tool(gateway, "list_agents")
    assert "alpha/w1:p1" in listed and "beta/w1:p1" in listed
    assert "Hello from beta." in tool(gateway, "send_prompt", agent_id="beta/w1:p1", text="hi", wait_seconds=5)
    assert "Hello from alpha." in tool(gateway, "send_prompt", agent_id="alpha/w1:p1", text="hi", wait_seconds=5)


def test_the_tunnel_gets_through_with_the_callers_address(hosts: tuple[Gateway, Bridge, Bridge]) -> None:
    gateway, _, _ = hosts
    # What cloudflared forwards from Cloudflare.
    headers = {"X-Forwarded-For": "203.0.113.9", "CF-Connecting-IP": "203.0.113.9"}

    assert httpx.post(f"{gateway.url}/mcp", headers=headers).status_code == 401
    assert httpx.get(f"{gateway.url}/.well-known/oauth-authorization-server", headers=headers).status_code == 200


def test_the_setup_scripts_start_the_gateway_without_proxy_headers() -> None:
    root = Path(__file__).parent.parent / "mcp-gateway"
    for path in (root / "deploy" / "setup.sh", root / "README.md"):
        starts = [line for line in path.read_text().splitlines() if "uvicorn mcp_gateway.server:main" in line
                  and "--host 0.0.0.0" in line]
        assert starts and all("--no-proxy-headers" in line for line in starts), path


def test_a_bridge_on_a_new_port_reports_it_and_one_leaves(
    hosts: tuple[Gateway, Bridge, Bridge], capsys: pytest.CaptureFixture
) -> None:
    gateway, alpha, beta = hosts
    string = gateway.open_enrollment(capsys)
    assert enroll(alpha, string, capsys)[0] == 0
    assert enroll(beta, string, capsys)[0] == 0

    beta.stop()
    beta.start()
    new_url = f"http://127.0.0.1:{BridgeSettings.from_env().port}"
    deadline = time.monotonic() + 10
    while next(host["url"] for host in gateway.hosts() if host["name"] == "beta") != new_url:
        assert time.monotonic() < deadline, "the gateway didn't take the new address"
        time.sleep(0.05)
    tool(gateway, "start_agent", project="beta-app")
    assert "beta/w1:p1" in tool(gateway, "list_agents")

    beta.use()
    assert bridge_cli.main(["unenroll"]) == 0
    assert [host["name"] for host in gateway.hosts()] == ["alpha"]
    assert load(beta.state_file).gateway is None
