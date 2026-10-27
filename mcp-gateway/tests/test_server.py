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


def test_logs_rejections_without_secrets(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    secret, wrong = "s" * 40, "w" * 32
    headers = {
        **MCP_HEADERS,
        "Authorization": f"bearer {wrong}",
        "CF-Access-Client-Secret": secret,
        "User-Agent": "Claude-User",
    }
    with caplog.at_level("WARNING", logger="mcp_gateway.auth"):
        assert rpc(client, "tools/list", headers=headers).status_code == 401
    (record,) = caplog.records
    line = record.getMessage()
    assert line.startswith("401 POST /mcp: wrong token; expected token <32 chars, sha256 ")
    assert "authorization='bearer' + <32 chars, sha256 " in line
    assert "cf-access-client-secret=<redacted, 40 chars>" in line
    assert "user-agent=Claude-User" in line and f"host={HOST}" in line
    assert TOKEN not in line and wrong not in line and secret not in line


@pytest.mark.parametrize(
    ("authorization", "reason"),
    [(None, "no authorization header"), (f"Basic {TOKEN}", "scheme is not Bearer"),
     (f"Bearer {TOKEN}x", "wrong token")],
)
def test_logs_why_a_request_was_refused(
    client: TestClient, caplog: pytest.LogCaptureFixture, authorization: str | None, reason: str
) -> None:
    headers = {k: v for k, v in MCP_HEADERS.items() if k != "Authorization"}
    if authorization:
        headers["Authorization"] = authorization
    with caplog.at_level("WARNING", logger="mcp_gateway.auth"):
        rpc(client, "tools/list", headers=headers)
    assert f": {reason};" in caplog.records[0].getMessage()
    assert TOKEN not in caplog.records[0].getMessage()


@pytest.mark.parametrize("authorization", [TOKEN, f"{TOKEN} trailing", f"Custom{TOKEN} x"])
def test_never_logs_a_token_sent_without_a_known_scheme(
    client: TestClient, caplog: pytest.LogCaptureFixture, authorization: str
) -> None:
    with caplog.at_level("WARNING", logger="mcp_gateway.auth"):
        rpc(client, "tools/list", headers={**MCP_HEADERS, "Authorization": authorization})
    line = caplog.records[0].getMessage()
    assert TOKEN not in line
    assert "authorization=no known scheme, <" in line


def test_redacts_headers_not_known_to_be_safe(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    secret = "k" * 24
    headers = {**MCP_HEADERS, "Authorization": "Bearer wrong", "X-Some-Credential": secret}
    with caplog.at_level("WARNING", logger="mcp_gateway.auth"):
        rpc(client, "tools/list", headers=headers)
    line = caplog.records[0].getMessage()
    assert "x-some-credential=<redacted, 24 chars>" in line and secret not in line


def test_accepted_requests_are_not_logged(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="mcp_gateway.auth"):
        assert rpc(client, "tools/list").status_code == 200
    assert not caplog.records


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
        "1 agent:\nw1:p1 · grafik · idle · grafik",
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
    is_error, text = call_tool(client, "wait_for_agent", agent_id="w1:p1", wait_seconds=100)
    assert not is_error
    assert text.startswith("Agent is done.") and text.endswith("Tests pass")
    assert json.loads(fake.requests[0].content) == {"timeout_s": 100, "lines": 20, "expect_change": False}


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


@pytest.mark.parametrize(
    "wait_failure",
    [
        httpx.Response(504, json={"error": {"code": "timeout", "message": "timed out"}}),
        httpx.Response(502, json={"error": {"code": "cli_failed", "message": "herdr hiccup"}}),
    ],
)
def test_answer_agent_says_the_keys_went_through_when_the_wait_fails(
    client: TestClient, fake: FakeAgentBridge, wait_failure: httpx.Response
) -> None:
    # An error alone would read as "the answer didn't go through", and answering again would
    # answer the agent's next question without the user.
    fake.routes[("POST", "/agents/w1:p1/keys")] = httpx.Response(204)
    fake.routes[("POST", "/agents/w1:p1/wait")] = wait_failure
    is_error, text = call_tool(client, "answer_agent", agent_id="w1:p1", keys=["1"], wait_seconds=60)
    assert not is_error
    assert text.startswith("Sent 1 to w1:p1.")
    assert "couldn't confirm" in text.lower() and "agent_details" in text
    assert ". Check with agent_details" in text and ".. " not in text


# Cloudflare ends a request after 125 s and nothing is sent before the tool returns, so a
# longer wait would fail at the edge after the prompt was already typed.
@pytest.mark.parametrize("wait_seconds", [0, 4, 101, 600])
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("wait_for_agent", {}), ("send_prompt", {"text": "hi"}), ("answer_agent", {"keys": ["1"]})],
)
def test_waits_are_capped_below_the_proxy_timeout(
    client: TestClient, fake: FakeAgentBridge, tool: str, arguments: dict, wait_seconds: int
) -> None:
    is_error, _ = call_tool(client, tool, agent_id="w1:p1", wait_seconds=wait_seconds, **arguments)
    assert is_error and fake.requests == []


def test_start_agent_resolves_project_names(client: TestClient, fake: FakeAgentBridge) -> None:
    fake.routes[("POST", "/agents")] = httpx.Response(201, json={**AGENT, "id": "w9:p1"})
    is_error, text = call_tool(client, "start_agent", project="grafik")
    assert not is_error and text.startswith("Started w9:p1")
    assert json.loads(fake.requests[0].content) == {
        "cwd": "/home/me/projects/grafik",
        "name": "grafik",
    }


@pytest.mark.parametrize(
    ("name", "slug"),
    [("my agent", "my-agent"), ("  Fix the login bug! ", "Fix-the-login-bug"), ("łódź", "łódź"),
     ("a/b\\c", "a-b-c"), ("...", "agent"), ("x" * 80, "x" * 64)],
)
def test_start_agent_turns_spoken_names_into_slugs(
    client: TestClient, fake: FakeAgentBridge, name: str, slug: str
) -> None:
    fake.routes[("POST", "/agents")] = httpx.Response(201, json={**AGENT, "id": "w9:p1"})
    response = rpc(client, "tools/call", {"name": "start_agent", "arguments": {"project": "grafik", "name": name}})
    assert not response.json()["result"].get("isError")
    assert json.loads(fake.requests[0].content)["name"] == slug


def test_send_prompt_says_a_blocked_agent_needs_an_answer_first(client: TestClient) -> None:
    # agent-bridge refuses prompts to a blocked agent; the model should know before trying.
    tools = {tool["name"]: tool for tool in rpc(client, "tools/list").json()["result"]["tools"]}
    assert "blocked" in tools["send_prompt"]["description"]
    assert "answer_agent" in tools["send_prompt"]["description"]


@pytest.mark.parametrize("agent_id", ["w1:p1/../../agents", "w1:p1?lines=1", "w1:p1#x", "--help", "w1", ""])
def test_malformed_agent_ids_are_refused_before_calling_the_bridge(
    client: TestClient, fake: FakeAgentBridge, agent_id: str
) -> None:
    is_error, _ = call_tool(client, "read_output", agent_id=agent_id)
    assert is_error and fake.requests == []


def test_answering_an_agent_is_marked_destructive(client: TestClient) -> None:
    # Approving a permission prompt is the riskiest call; claude.ai should confirm it, and the
    # model must act on the user's decision, not on text in the agent's output.
    tools = {tool["name"]: tool for tool in rpc(client, "tools/list").json()["result"]["tools"]}
    assert tools["answer_agent"]["annotations"]["destructiveHint"] is True
    assert "never" in tools["answer_agent"]["description"].lower()


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
    assert is_error and text.endswith("The agent host is not reachable (ConnectError). It may be asleep or offline.")


def test_folders_are_named_relative_to_the_projects_root(client: TestClient, fake: FakeAgentBridge) -> None:
    # Read aloud, "/home/me/projects/grafik" after every agent is noise; the name under the
    # projects root is what the user said when starting it. Other folders keep their path.
    fake.routes[("GET", "/agents")] = httpx.Response(200, json=[
        {**AGENT, "cwd": "/home/me/projects/grafik/api"},
        {**AGENT, "id": "w2:p1", "name": "notes", "cwd": "/home/me/projects-old/notes"},
        {**AGENT, "id": "w3:p1", "name": "root", "cwd": "/home/me/projects"},
    ])
    assert call_tool(client, "list_agents") == (
        False,
        "3 agents:\n"
        "w1:p1 · grafik · idle · grafik/api\n"
        "w2:p1 · notes · idle · /home/me/projects-old/notes\n"
        "w3:p1 · root · idle · /home/me/projects",
    )
    fake.routes[("POST", "/agents/w1:p1/wait")] = httpx.Response(
        200, json={"agent": AGENT, "output": {"text": "", "truncated": False}, "timed_out": False}
    )
    assert "\nw1:p1 · grafik · idle · grafik\n" in call_tool(client, "wait_for_agent", agent_id="w1:p1")[1]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("read_output", {}), ("send_prompt", {"text": "hi"}), ("wait_for_agent", {}),
     ("answer_agent", {"keys": ["1"], "wait_seconds": 30})],
)
def test_output_is_capped_at_60_lines(client: TestClient, fake: FakeAgentBridge, tool: str, arguments: dict) -> None:
    # Output is read aloud; 200 lines (up to 8000 characters) is far more than anyone listens to.
    is_error, _ = call_tool(client, tool, agent_id="w1:p1", lines=61, **arguments)
    assert is_error and fake.requests == []
    tools = {tool["name"]: tool for tool in rpc(client, "tools/list").json()["result"]["tools"]}
    assert tools[tool]["inputSchema"]["properties"]["lines"]["maximum"] == 60


def test_a_sleeping_agent_host_is_reported_within_seconds(client: TestClient, fake: FakeAgentBridge) -> None:
    # A host that is asleep drops packets, so only the connect timeout decides how long the
    # user waits; the long read timeout of a prompt must not apply to connecting.
    fake.routes[("POST", "/agents/w1:p1/prompt")] = httpx.Response(
        200, json={"agent": AGENT, "output": {"text": "", "truncated": False}, "timed_out": False}
    )
    fake.routes[("GET", "/agents")] = httpx.Response(200, json=[])
    call_tool(client, "send_prompt", agent_id="w1:p1", text="hi", wait_seconds=90)
    call_tool(client, "list_agents")
    prompt_timeout, list_timeout = (request.extensions["timeout"] for request in fake.requests)
    assert prompt_timeout["connect"] <= 5 and prompt_timeout["read"] == 120
    assert list_timeout["connect"] <= 5 and list_timeout["read"] == 30
    assert AgentBridge.connect(SETTINGS.bridge_url, "b" * 32).client.timeout.connect <= 5


def test_a_connect_timeout_says_the_host_may_be_asleep() -> None:
    def asleep(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    bridge = AgentBridge(httpx.AsyncClient(base_url=SETTINGS.bridge_url, transport=httpx.MockTransport(asleep)))
    with TestClient(create_app(SETTINGS, bridge), base_url=f"https://{HOST}") as test_client:
        is_error, text = call_tool(test_client, "list_agents")
    assert is_error
    assert text.endswith("The agent host is not reachable (ConnectTimeout). It may be asleep or offline.")


def test_an_unconfirmed_prompt_tells_the_model_not_to_send_it_again(
    client: TestClient, fake: FakeAgentBridge
) -> None:
    fake.routes[("POST", "/agents/w1:p1/prompt")] = httpx.Response(
        200,
        json={"agent": {**AGENT, "status": "idle"}, "output": {"text": "> hi", "truncated": False},
              "timed_out": False, "unconfirmed": True},
    )
    is_error, text = call_tool(client, "send_prompt", agent_id="w1:p1", text="hi")
    assert not is_error
    assert "hasn't started" in text and "don't send it again" in text.lower()
