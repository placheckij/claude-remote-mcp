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
    projects_root="/home/me/projects",
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
    "cwd": "/home/me/projects/grafik",
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
def fake() -> FakeAgentBridge:
    return FakeAgentBridge({})


@pytest.fixture
def client(fake: FakeAgentBridge):
    bridge = AgentBridge(
        httpx.AsyncClient(base_url=SETTINGS.bridge_url, transport=httpx.MockTransport(fake),
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
    "authorization",
    [
        None, "", "Bearer", "Bearer ", "Bearer wrong", TOKEN, f"Bearer {TOKEN}x", f"Bearer  {TOKEN}",
        f"Bearer {TOKEN.upper()}", f"Basic {TOKEN}", f"Bearer{TOKEN}",
    ],
)
def test_rejects_requests_without_the_exact_token(client: TestClient, authorization) -> None:
    headers = {k: v for k, v in MCP_HEADERS.items() if k != "Authorization"}
    if authorization is not None:
        headers["Authorization"] = authorization
    response = rpc(client, "tools/list", headers=headers)
    assert response.status_code == 401


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_accepts_the_token_with_any_scheme_case(client: TestClient, scheme: str) -> None:
    response = rpc(client, "tools/list", headers={**MCP_HEADERS, "Authorization": f"{scheme} {TOKEN}"})
    assert response.status_code == 200


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
        "list_agents", "agent_details", "read_output", "send_prompt", "wait_for_agent",
        "answer_agent", "start_agent", "stop_agent",
    }


def test_list_agents_is_one_line_per_agent(client: TestClient, fake: FakeAgentBridge) -> None:
    fake.routes[("GET", "/agents")] = httpx.Response(200, json=[AGENT])
    assert call_tool(client, "list_agents") == (
        False,
        "1 agents:\nw1:p1 · grafik · idle · /home/me/projects/grafik",
    )


def test_send_prompt_reports_status_and_output(client: TestClient, fake: FakeAgentBridge) -> None:
    fake.routes[("POST", "/agents/w1:p1/prompt")] = httpx.Response(
        200,
        json={"agent": {**AGENT, "status": "done"}, "output": {"text": "⏺ PONG", "truncated": False},
              "timed_out": False},
    )
    is_error, text = call_tool(client, "send_prompt", agent_id="w1:p1", text="say pong", wait_seconds=30)
    assert not is_error
    assert text.startswith("Agent is done.")
    assert text.endswith("⏺ PONG")
    assert json.loads(fake.requests[0].content) == {"text": "say pong", "timeout_s": 30, "lines": 20}


def test_send_prompt_flags_blocked_and_timed_out(client: TestClient, fake: FakeAgentBridge) -> None:
    route = ("POST", "/agents/w1:p1/prompt")
    output = {"text": "Allow rm?", "truncated": True}
    fake.routes[route] = httpx.Response(
        200, json={"agent": {**AGENT, "status": "blocked"}, "output": output, "timed_out": False}
    )
    assert "waiting for an approval" in call_tool(client, "send_prompt", agent_id="w1:p1", text="x")[1]
    fake.routes[route] = httpx.Response(
        200, json={"agent": {**AGENT, "status": "working"}, "output": output, "timed_out": True}
    )
    text = call_tool(client, "send_prompt", agent_id="w1:p1", text="x")[1]
    assert text.startswith("Still working after 90s. Keep waiting with wait_for_agent.")
    assert "(earlier output cut)" in text


def test_wait_for_agent_reports_how_it_ended(client: TestClient, fake: FakeAgentBridge) -> None:
    fake.routes[("POST", "/agents/w1:p1/wait")] = httpx.Response(
        200,
        json={"agent": {**AGENT, "status": "done"}, "output": {"text": "Tests pass", "truncated": False},
              "timed_out": False},
    )
    is_error, text = call_tool(client, "wait_for_agent", agent_id="w1:p1", wait_seconds=120)
    assert not is_error
    assert text.startswith("Agent is done.") and text.endswith("Tests pass")
    assert json.loads(fake.requests[0].content) == {"timeout_s": 120, "lines": 20, "expect_change": False}


def test_wait_for_unknown_agent_passes_the_error_through(client: TestClient, fake: FakeAgentBridge) -> None:
    fake.routes[("POST", "/agents/w9:p9/wait")] = httpx.Response(
        404, json={"error": {"code": "agent_not_found", "message": "agent target w9:p9 not found"}}
    )
    is_error, text = call_tool(client, "wait_for_agent", agent_id="w9:p9")
    assert is_error and text.endswith("agent target w9:p9 not found")


def test_answer_agent_only_sends_keys_by_default(client: TestClient, fake: FakeAgentBridge) -> None:
    fake.routes[("POST", "/agents/w1:p1/keys")] = httpx.Response(204)
    assert call_tool(client, "answer_agent", agent_id="w1:p1", keys=["1"]) == (False, "Sent 1 to w1:p1.")
    assert [request.url.path for request in fake.requests] == ["/agents/w1:p1/keys"]


def test_answer_agent_can_wait_for_the_result(client: TestClient, fake: FakeAgentBridge) -> None:
    fake.routes[("POST", "/agents/w1:p1/keys")] = httpx.Response(204)
    fake.routes[("POST", "/agents/w1:p1/wait")] = httpx.Response(
        200,
        json={"agent": {**AGENT, "status": "idle"}, "output": {"text": "Removed.", "truncated": False},
              "timed_out": False},
    )
    is_error, text = call_tool(client, "answer_agent", agent_id="w1:p1", keys=["1"], wait_seconds=60)
    assert not is_error
    assert text.startswith("Sent 1 to w1:p1.\nAgent is idle.") and text.endswith("Removed.")
    assert [request.url.path for request in fake.requests] == ["/agents/w1:p1/keys", "/agents/w1:p1/wait"]
    # The agent is still "blocked" right after the key press; agent-bridge must skip that.
    assert json.loads(fake.requests[1].content)["expect_change"] is True


@pytest.mark.parametrize("wait_seconds", [0, 4, 601])
def test_wait_rejects_out_of_range_waits(client: TestClient, fake: FakeAgentBridge, wait_seconds: int) -> None:
    is_error, _ = call_tool(client, "wait_for_agent", agent_id="w1:p1", wait_seconds=wait_seconds)
    assert is_error and fake.requests == []


def test_start_agent_resolves_project_names(client: TestClient, fake: FakeAgentBridge) -> None:
    fake.routes[("POST", "/agents")] = httpx.Response(201, json={**AGENT, "id": "w9:p1"})
    is_error, text = call_tool(client, "start_agent", project="grafik")
    assert not is_error and text.startswith("Started w9:p1")
    assert json.loads(fake.requests[0].content) == {
        "cwd": "/home/me/projects/grafik",
        "name": "grafik",
    }


def test_refusals_from_the_bridge_reach_the_model_verbatim(client: TestClient, fake: FakeAgentBridge) -> None:
    message = (
        "Refusing to start a new agent: 10 agents are already running "
        "and the limit is 10. Stop an agent first."
    )
    fake.routes[("POST", "/agents")] = httpx.Response(
        409, json={"error": {"code": "agent_limit_reached", "message": message}}
    )
    is_error, text = call_tool(client, "start_agent", project="grafik")
    assert is_error and text.endswith(message)

    fake.routes[("POST", "/agents")] = httpx.Response(
        403, json={"detail": "/etc is outside the allowed roots"}
    )
    is_error, text = call_tool(client, "start_agent", project="/etc")
    assert is_error and text.endswith("/etc is outside the allowed roots")


def test_unreachable_agent_host_is_reported_plainly(client: TestClient, fake: FakeAgentBridge) -> None:
    fake.down = True
    is_error, text = call_tool(client, "list_agents")
    assert is_error and text.endswith("The agent host is not reachable (ConnectError).")
