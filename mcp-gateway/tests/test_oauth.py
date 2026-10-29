"""The OAuth sign-in, driven the way claude.ai does it: discovery from the 401, dynamic
client registration, /authorize, the sign-in page, then tokens on /mcp."""

import base64
import hashlib
import json
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.testclient import TestClient

from mcp_gateway.bridge import AgentBridge
from mcp_gateway.config import Settings
from mcp_gateway.oauth import (
    ACCESS_TOKEN_TTL_S,
    FAILURE_WINDOW_S,
    MAX_FAILURES,
    MAX_TRIES_PER_SIGN_IN,
    GatewayOAuth,
)
from mcp_gateway.server import create_app

HOST = "mcp.example.com"
RESOURCE = f"https://{HOST}/mcp"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PASSPHRASE = "correct horse battery staple"
STATIC_TOKEN = "s" * 32
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "Host": HOST}


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def settings(state_dir: Path, token: str | None = None) -> Settings:
    return Settings(
        token=token,
        oauth_passphrase=PASSPHRASE,
        state_dir=state_dir,
        bridge_url="http://agents.lan:8765",
        bridge_token="b" * 32,
        public_host=HOST,
        projects_root="/home/me/projects",
    )


def bridge() -> AgentBridge:
    agents = httpx.Response(200, json=[])
    return AgentBridge(
        httpx.AsyncClient(base_url="http://agents.lan:8765", transport=httpx.MockTransport(lambda _: agents))
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    # The SDK checks code and token expiry against the real clock.
    monkeypatch.setattr("time.time", clock)
    return clock


@pytest.fixture
def make_client(tmp_path: Path, clock: Clock):
    clients = []

    def make(token: str | None = None) -> TestClient:
        oauth = GatewayOAuth(settings(tmp_path, token), clock=clock)
        client = TestClient(
            create_app(settings(tmp_path, token), bridge(), oauth),
            base_url=f"https://{HOST}",
            follow_redirects=False,
        )
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


def list_tools(client: TestClient, access_token: str | None) -> httpx.Response:
    headers = {**MCP_HEADERS, **({"Authorization": f"Bearer {access_token}"} if access_token else {})}
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


def register(client: TestClient, redirect_uri: str = CALLBACK) -> dict:
    response = client.post(
        "/register",
        json={
            "client_name": "Claude",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    return response.json() | {"status": response.status_code}


def start_sign_in(client: TestClient, client_id: str, **overrides) -> httpx.Response:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CALLBACK,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "state-123",
        "scope": "agents",
        "resource": RESOURCE,
    } | overrides
    return client.get("/authorize", params=params)


def login_request_id(response: httpx.Response) -> str:
    assert response.status_code == 302, response.text
    location = urlsplit(response.headers["location"])
    assert (location.netloc, location.path) == (HOST, "/login")
    return parse_qs(location.query)["request"][0]


def sign_in(
    client: TestClient, request_id: str, passphrase: str = PASSPHRASE, source: str = "198.51.100.7"
) -> httpx.Response:
    # Cloudflare sets CF-Connecting-IP to the address it saw.
    return client.post(
        "/login", data={"request": request_id, "passphrase": passphrase}, headers={"CF-Connecting-IP": source}
    )


def code_from(response: httpx.Response) -> str:
    assert response.status_code == 302, response.text
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == CALLBACK
    query = parse_qs(location.query)
    assert query["state"] == ["state-123"]
    return query["code"][0]


def exchange(client: TestClient, client_id: str, code: str) -> httpx.Response:
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "client_id": client_id,
            "code_verifier": VERIFIER,
            "resource": RESOURCE,
        },
    )


def refresh(client: TestClient, client_id: str, refresh_token: str) -> httpx.Response:
    return client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
    )


def connect(client: TestClient) -> tuple[str, dict]:
    """The whole sign-in; returns the client id and the token response."""
    client_id = register(client)["client_id"]
    code = code_from(sign_in(client, login_request_id(start_sign_in(client, client_id))))
    response = exchange(client, client_id, code)
    assert response.status_code == 200, response.text
    return client_id, response.json()


# --- Discovery --------------------------------------------------------------------------


def test_discovery_starts_from_the_401(client: TestClient) -> None:
    response = list_tools(client, None)
    assert response.status_code == 401
    metadata_url = response.headers["www-authenticate"].split('resource_metadata="')[1].rstrip('"')
    resource = client.get(urlsplit(metadata_url).path).json()
    # claude.ai needs `resource` to be the connector URL exactly, and uses the first server.
    assert resource["resource"] == RESOURCE
    assert resource["authorization_servers"][0] == f"https://{HOST}"
    assert resource["scopes_supported"] == ["agents"]

    server = client.get("/.well-known/oauth-authorization-server").json()
    assert server["issuer"] == f"https://{HOST}"
    assert server["registration_endpoint"] == f"https://{HOST}/register"
    assert server["code_challenge_methods_supported"] == ["S256"]


# --- The whole sign-in ------------------------------------------------------------------


def test_signing_in_gives_tokens_that_open_the_mcp_endpoint(client: TestClient) -> None:
    _, tokens = connect(client)
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == ACCESS_TOKEN_TTL_S
    assert tokens["refresh_token"]
    assert list_tools(client, tokens["access_token"]).status_code == 200


def test_the_sign_in_page_says_who_asks_and_where_it_returns(client: TestClient) -> None:
    client_id = register(client)["client_id"]
    request_id = login_request_id(start_sign_in(client, client_id))
    page = client.get("/login", params={"request": request_id})
    assert page.status_code == 200
    assert "<strong>Claude</strong> wants to control" in page.text
    assert "you go back to <strong>claude.ai</strong>" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "form-action 'self' https://claude.ai" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"


def test_a_client_name_cannot_inject_markup(client: TestClient) -> None:
    response = client.post(
        "/register",
        json={"client_name": "<script>x</script>", "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"},
    )
    request_id = login_request_id(start_sign_in(client, response.json()["client_id"]))
    page = client.get("/login", params={"request": request_id}).text
    assert "<script>" not in page and "&lt;script&gt;" in page


def test_tokens_survive_a_restart(make_client) -> None:
    client_id, tokens = connect(make_client())
    restarted = make_client()
    assert list_tools(restarted, tokens["access_token"]).status_code == 200
    assert refresh(restarted, client_id, tokens["refresh_token"]).status_code == 200


def test_only_hashes_reach_the_disk(client: TestClient, tmp_path: Path) -> None:
    _, tokens = connect(client)
    state = tmp_path / "oauth.json"
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    text = state.read_text()
    assert tokens["access_token"] not in text and tokens["refresh_token"] not in text
    assert hashlib.sha256(tokens["access_token"].encode()).hexdigest() in text


# --- Refresh, expiry, revocation --------------------------------------------------------


def test_refresh_replaces_both_tokens(client: TestClient) -> None:
    client_id, first = connect(client)
    response = refresh(client, client_id, first["refresh_token"])
    assert response.status_code == 200, response.text
    second = response.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert list_tools(client, second["access_token"]).status_code == 200
    # The old pair is gone; claude.ai is told with invalid_grant, as its docs require.
    assert list_tools(client, first["access_token"]).status_code == 401
    reused = refresh(client, client_id, first["refresh_token"])
    assert (reused.status_code, reused.json()["error"]) == (400, "invalid_grant")


def test_an_expired_access_token_is_refused_and_refresh_still_works(client: TestClient, clock: Clock) -> None:
    client_id, tokens = connect(client)
    clock.now += ACCESS_TOKEN_TTL_S + 1
    assert list_tools(client, tokens["access_token"]).status_code == 401
    renewed = refresh(client, client_id, tokens["refresh_token"]).json()
    assert list_tools(client, renewed["access_token"]).status_code == 200


def test_a_refresh_token_belongs_to_its_client(client: TestClient) -> None:
    _, tokens = connect(client)
    other = register(client)["client_id"]
    response = refresh(client, other, tokens["refresh_token"])
    assert (response.status_code, response.json()["error"]) == (400, "invalid_grant")


def test_revoking_ends_both_tokens(client: TestClient) -> None:
    client_id, tokens = connect(client)
    # The SDK's revocation form requires client_secret, even empty for a public client.
    response = client.post(
        "/revoke", data={"token": tokens["refresh_token"], "client_id": client_id, "client_secret": ""}
    )
    assert response.status_code == 200
    assert list_tools(client, tokens["access_token"]).status_code == 401
    assert refresh(client, client_id, tokens["refresh_token"]).json()["error"] == "invalid_grant"


# --- Refusals ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "redirect_uri",
    ["https://evil.example/callback", "https://claude.ai/api/mcp/auth_callback/x", "http://claude.ai/api/mcp/auth_callback",
     "http://localhost:3118/callback"],
)
def test_only_claudes_callback_can_be_registered(client: TestClient, redirect_uri: str) -> None:
    # Otherwise anyone could register their own callback and phish the passphrase's code.
    response = register(client, redirect_uri)
    assert (response["status"], response["error"]) == (400, "invalid_redirect_uri")


def test_a_wrong_passphrase_issues_nothing(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    client_id = register(client)["client_id"]
    request_id = login_request_id(start_sign_in(client, client_id))
    with caplog.at_level("WARNING", logger="mcp_gateway.oauth"):
        response = sign_in(client, request_id, "wrong passphrase!")
    assert response.status_code == 403
    assert "Wrong passphrase." in response.text
    assert PASSPHRASE not in caplog.text and "wrong passphrase!" not in caplog.text
    # The same sign-in can still be finished with the right one.
    assert code_from(sign_in(client, request_id))


def test_a_sign_in_link_dies_after_a_few_wrong_passphrases(client: TestClient) -> None:
    client_id = register(client)["client_id"]
    request_id = login_request_id(start_sign_in(client, client_id))
    for _ in range(MAX_TRIES_PER_SIGN_IN - 1):
        assert "Wrong passphrase." in sign_in(client, request_id, "guess guess guess").text
    last = sign_in(client, request_id, "guess guess guess")
    assert last.status_code == 403 and "Connect again" in last.text
    # Not even the right passphrase finishes it now.
    assert sign_in(client, request_id).status_code == 400


def test_wrong_passphrases_lock_out_their_source_only(client: TestClient, clock: Clock) -> None:
    # Anyone can start a sign-in, so a global lock would let a stranger keep the owner out.
    client_id = register(client)["client_id"]
    guesser = "203.0.113.9"
    for _ in range(MAX_FAILURES // MAX_TRIES_PER_SIGN_IN):
        request_id = login_request_id(start_sign_in(client, client_id))
        for _ in range(MAX_TRIES_PER_SIGN_IN):
            assert sign_in(client, request_id, "guess guess guess", source=guesser).status_code == 403
    request_id = login_request_id(start_sign_in(client, client_id))
    # Even the right passphrase is refused from there now,
    assert sign_in(client, request_id, source=guesser).status_code == 429
    # but the owner, elsewhere, signs in.
    assert code_from(sign_in(client, request_id))

    clock.now += FAILURE_WINDOW_S + 1
    request_id = login_request_id(start_sign_in(client, client_id))
    assert code_from(sign_in(client, request_id, source=guesser))


def test_a_sign_in_link_works_once_and_expires(client: TestClient, clock: Clock) -> None:
    client_id = register(client)["client_id"]
    request_id = login_request_id(start_sign_in(client, client_id))
    code_from(sign_in(client, request_id))
    assert sign_in(client, request_id).status_code == 400

    request_id = login_request_id(start_sign_in(client, client_id))
    clock.now += 601
    response = client.get("/login", params={"request": request_id})
    assert response.status_code == 400 and "expired" in response.text
    assert client.get("/login", params={"request": "made-up"}).status_code == 400


def test_a_code_is_good_for_one_exchange(client: TestClient) -> None:
    client_id = register(client)["client_id"]
    code = code_from(sign_in(client, login_request_id(start_sign_in(client, client_id))))
    assert exchange(client, client_id, code).status_code == 200
    second = exchange(client, client_id, code)
    assert (second.status_code, second.json()["error"]) == (400, "invalid_grant")


def test_the_code_needs_the_pkce_verifier(client: TestClient) -> None:
    client_id = register(client)["client_id"]
    code = code_from(sign_in(client, login_request_id(start_sign_in(client, client_id))))
    response = client.post(
        "/token",
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
              "client_id": client_id, "code_verifier": "w" * 64},
    )
    assert (response.status_code, response.json()["error"]) == (400, "invalid_grant")


def test_tokens_for_another_resource_are_refused(client: TestClient) -> None:
    client_id = register(client)["client_id"]
    response = start_sign_in(client, client_id, resource="https://other.example.com/mcp")
    location = parse_qs(urlsplit(response.headers["location"]).query)
    assert location["error"] == ["invalid_target"]


def test_signing_in_without_a_resource_still_works(client: TestClient) -> None:
    # RFC 8707's resource is optional; the token is for this server either way.
    client_id = register(client)["client_id"]
    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
        "code_challenge": CHALLENGE, "code_challenge_method": "S256", "state": "state-123",
    }
    code = code_from(sign_in(client, login_request_id(client.get("/authorize", params=params))))
    tokens = exchange(client, client_id, code).json()
    assert list_tools(client, tokens["access_token"]).status_code == 200


def test_unfinished_registrations_are_not_written_down(client: TestClient, tmp_path: Path) -> None:
    for _ in range(3):
        register(client)
    assert not (tmp_path / "oauth.json").exists()
    connect(client)
    assert len(json.loads((tmp_path / "oauth.json").read_text())["clients"]) == 1


def test_an_unreadable_state_file_stops_startup_with_the_fix(tmp_path: Path) -> None:
    (tmp_path / "oauth.json").write_text("{not json")
    with pytest.raises(RuntimeError, match="Deleting it signs the connector out"):
        GatewayOAuth(settings(tmp_path))


# --- The static token -------------------------------------------------------------------


def test_the_static_token_works_while_set(make_client) -> None:
    assert list_tools(make_client(token=STATIC_TOKEN), STATIC_TOKEN).status_code == 200


def test_the_static_token_is_refused_once_unset(client: TestClient) -> None:
    assert list_tools(client, STATIC_TOKEN).status_code == 401
