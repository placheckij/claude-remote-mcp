import json

import httpx
import pytest
from starlette.testclient import TestClient

from mcp_gateway.bridge import AgentBridge
from mcp_gateway.config import Settings
from mcp_gateway.server import create_app

TOKEN = "g" * 32
HOST = "mcp.example.com"
SETTINGS = Settings(
    token=TOKEN,
    bridge_url="http://agents.lan:8765",
    bridge_token="b" * 32,
    public_host=HOST,
)
MCP_HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "Host": HOST,
}

AGENT = {
    "id": "w1:p1",
    "name": "grafik",
    "title": "repo-1",
    "status": "idle",
    "cwd": "/Users/me/projects/grafik",
    "workspace_id": "w1",
}


class FakeAgentBridge:
    """Stands in for agent-bridge; records requests and answers from a route table."""

    def __init__(self, routes: dict[tuple[str, str], httpx.Response]):
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        self.requests.append(request)
        assert request.headers["authorization"] == f"Bearer {'b' * 32}"
        return self.routes[(request.method, request.url.path)]


@pytest.fixture
def mac() -> FakeAgentBridge:
    return FakeAgentBridge({})


@pytest.fixture
def client(mac: FakeAgentBridge):
    bridge = AgentBridge(
        httpx.AsyncClient(base_url=SETTINGS.bridge_url, transport=httpx.MockTransport(mac),
                          headers={"Authorization": f"Bearer {'b' * 32}"})
    )
    with TestClient(create_app(SETTINGS, bridge), base_url=f"https://{HOST}") as test_client:
        yield test_client


def rpc(client: TestClient, method: str, params: dict | None = None, headers=MCP_HEADERS):
    return client.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    )


def call_tool(client: TestClient, name: str, **arguments) -> tuple[bool, str]:
    response = rpc(client, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return result.get("isError", False), result["content"][0]["text"]


# --- Access control -------------------------------------------------------------------


@pytest.mark.parametrize(
    "authorization", [None, "", "Bearer", "Bearer wrong", TOKEN, f"Bearer {TOKEN}x", f"bearer {TOKEN}"]
)
def test_rejects_requests_without_the_exact_token(client: TestClient, authorization) -> None:
    headers = {k: v for k, v in MCP_HEADERS.items() if k != "Authorization"}
    if authorization is not None:
        headers["Authorization"] = authorization
    response = rpc(client, "tools/list", headers=headers)
    assert response.status_code == 401


def test_rejects_unknown_host(client: TestClient) -> None:
    response = rpc(client, "tools/list", headers={**MCP_HEADERS, "Host": "evil.example.com"})
    assert response.status_code == 421


def test_health_check_is_open_and_says_nothing(client: TestClient) -> None:
    response = client.get("/healthz")
    assert (response.status_code, response.text) == (200, "ok")


# --- Tools ----------------------------------------------------------------------------


def test_lists_the_tools(client: TestClient) -> None:
    tools = {tool["name"] for tool in rpc(client, "tools/list").json()["result"]["tools"]}
    assert tools == {
        "list_agents", "agent_details", "read_output", "send_prompt",
        "answer_agent", "start_agent", "stop_agent",
    }


def test_list_agents_is_one_line_per_agent(client: TestClient, mac: FakeAgentBridge) -> None:
    mac.routes[("GET", "/agents")] = httpx.Response(200, json=[AGENT])
    assert call_tool(client, "list_agents") == (
        False,
        "1 agents:\nw1:p1 · grafik · idle · /Users/me/projects/grafik",
    )


def test_send_prompt_reports_status_and_output(client: TestClient, mac: FakeAgentBridge) -> None:
    mac.routes[("POST", "/agents/w1:p1/prompt")] = httpx.Response(
        200,
        json={"agent": {**AGENT, "status": "done"}, "output": {"text": "⏺ PONG", "truncated": False},
              "timed_out": False},
    )
    is_error, text = call_tool(client, "send_prompt", agent_id="w1:p1", text="say pong", wait_seconds=30)
    assert not is_error
    assert text.startswith("Agent is done.")
    assert text.endswith("⏺ PONG")
    assert json.loads(mac.requests[0].content) == {"text": "say pong", "timeout_s": 30, "lines": 20}


def test_send_prompt_flags_blocked_and_timed_out(client: TestClient, mac: FakeAgentBridge) -> None:
    route = ("POST", "/agents/w1:p1/prompt")
    output = {"text": "Allow rm?", "truncated": True}
    mac.routes[route] = httpx.Response(
        200, json={"agent": {**AGENT, "status": "blocked"}, "output": output, "timed_out": False}
    )
    assert "waiting for an approval" in call_tool(client, "send_prompt", agent_id="w1:p1", text="x")[1]
    mac.routes[route] = httpx.Response(
        200, json={"agent": {**AGENT, "status": "working"}, "output": output, "timed_out": True}
    )
    text = call_tool(client, "send_prompt", agent_id="w1:p1", text="x")[1]
    assert text.startswith("Still working after 90s")
    assert "(earlier output cut)" in text


def test_start_agent_resolves_project_names(client: TestClient, mac: FakeAgentBridge) -> None:
    mac.routes[("POST", "/agents")] = httpx.Response(201, json={**AGENT, "id": "w9:p1"})
    is_error, text = call_tool(client, "start_agent", project="grafik")
    assert not is_error and text.startswith("Started w9:p1")
    assert json.loads(mac.requests[0].content) == {
        "cwd": "/Users/me/projects/grafik",
        "name": "grafik",
    }


def test_refusals_from_the_mac_reach_the_model_verbatim(client: TestClient, mac: FakeAgentBridge) -> None:
    message = (
        "Refusing to start a new agent: 10 agents are already running "
        "and the limit is 10. Stop an agent first."
    )
    mac.routes[("POST", "/agents")] = httpx.Response(
        409, json={"error": {"code": "agent_limit_reached", "message": message}}
    )
    is_error, text = call_tool(client, "start_agent", project="grafik")
    assert is_error and text.endswith(message)

    mac.routes[("POST", "/agents")] = httpx.Response(
        403, json={"detail": "/etc is outside the allowed roots"}
    )
    is_error, text = call_tool(client, "start_agent", project="/etc")
    assert is_error and text.endswith("/etc is outside the allowed roots")


def test_unreachable_mac_is_reported_plainly(client: TestClient, mac: FakeAgentBridge) -> None:
    mac.down = True
    is_error, text = call_tool(client, "list_agents")
    assert is_error and text.endswith("The agent host is not reachable (ConnectError).")
