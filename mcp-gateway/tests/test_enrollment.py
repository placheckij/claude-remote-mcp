"""Agent-bridges adding themselves: the window, the routes and who may call them."""

import json
import logging
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from mcp_gateway import cli
from mcp_gateway.bridge import AgentBridge
from mcp_gateway.config import Settings
from mcp_gateway.enrollment import MAX_FAILURES, WINDOW_S, Enrollment, is_lan
from mcp_gateway.registry import Registry
from mcp_gateway.server import create_app

TOKEN = "b" * 32
BRIDGE_ID = "1" * 32
URL = "http://192.168.1.20:8765"
LAN = ("192.168.1.20", 50000)
INFO = {
    "api_version": 2, "bridge_id": BRIDGE_ID, "name": "mac", "platform": "darwin", "bridge_version": "0.1.0",
    "herdr_version": "0.9.5", "roots": ["/Users/me/projects"], "projects_root": "/Users/me/projects",
    "accounts": [], "max_agents": 10,
}


class Gateway:
    """The gateway app with a clock, and fake bridges answering /info by URL."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.now = 1_000_000.0
        self.enrollment = Enrollment(state_dir / "enrollment.json", clock=lambda: self.now)
        # URL → what its /info answers; a URL not here refuses connections.
        self.bridges: dict[str, httpx.Response] = {URL: httpx.Response(200, json=INFO)}
        self.asked: list[httpx.Request] = []
        settings = Settings(oauth_passphrase="p" * 24, state_dir=state_dir, public_host="mcp.example.com")
        self.app = create_app(settings, self.connect, enrollment=self.enrollment)

    def connect(self, url: str, token: str) -> AgentBridge:
        def answer(request: httpx.Request) -> httpx.Response:
            self.asked.append(request)
            if url not in self.bridges:
                raise httpx.ConnectError("connection refused", request=request)
            return self.bridges[url]

        return AgentBridge(httpx.AsyncClient(base_url=url, transport=httpx.MockTransport(answer),
                                             headers={"Authorization": f"Bearer {token}"}))

    def client(self, peer: tuple[str, int] = LAN) -> TestClient:
        # Not entered: the routes need no lifespan, which the MCP app allows only once.
        return TestClient(self.app, base_url="http://192.168.1.10:8766", client=peer)

    def open(self) -> str:
        secret, _ = self.enrollment.open()
        return secret

    def enroll(self, secret: str, peer=LAN, headers=None, **fields) -> httpx.Response:
        body = {"secret": secret, "bridge_id": BRIDGE_ID, "url": URL, "token": TOKEN, "name": "mac", **fields}
        return self.client(peer).post("/enroll", json=body, headers=headers or {})

    def hosts(self) -> list[dict]:
        path = self.state_dir / "bridges.json"
        return json.loads(path.read_text())["bridges"] if path.exists() else []


@pytest.fixture
def gateway(tmp_path: Path) -> Gateway:
    return Gateway(tmp_path)


def test_enrolls_a_bridge_that_answers_as_itself(gateway: Gateway, caplog: pytest.LogCaptureFixture) -> None:
    response = gateway.enroll(gateway.open())

    assert response.status_code == 200, response.text
    assert response.json() == {"name": "mac", "gateway_id": gateway.enrollment.gateway_id(), "api_version": 2}
    [host] = gateway.hosts()
    assert (host["bridge_id"], host["name"], host["url"], host["token"]) == (BRIDGE_ID, "mac", URL, TOKEN)
    assert host["info"] == INFO
    # The gateway asked the bridge with the bridge's own token.
    assert gateway.asked[0].headers["authorization"] == f"Bearer {TOKEN}"
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING and "Enrolled" in r.message]
    assert "mac" in warning.message and "192.168.1.20" in warning.message


def test_the_window_takes_any_number_of_bridges(gateway: Gateway) -> None:
    secret = gateway.open()
    gateway.bridges["http://192.168.1.21:8765"] = httpx.Response(200, json={**INFO, "bridge_id": "2" * 32})

    assert gateway.enroll(secret).status_code == 200
    second = gateway.enroll(secret, bridge_id="2" * 32, url="http://192.168.1.21:8765", name="linux")
    assert second.status_code == 200, second.text
    assert [host["name"] for host in gateway.hosts()] == ["mac", "linux"]


def test_refuses_a_wrong_secret(gateway: Gateway) -> None:
    gateway.open()
    response = gateway.enroll("0" * 24)

    assert (response.status_code, response.json()["error"]["code"]) == (401, "wrong_secret")
    assert gateway.hosts() == []
    # Nothing was asked of the URL given.
    assert gateway.asked == []


def test_refuses_without_a_window_and_after_it_ends(gateway: Gateway) -> None:
    assert gateway.enroll("0" * 24).json()["error"]["code"] == "enrollment_closed"

    secret = gateway.open()
    gateway.now += WINDOW_S
    response = gateway.enroll(secret)

    assert (response.status_code, response.json()["error"]["code"]) == (401, "enrollment_closed")
    assert "sudo mcp-gateway enroll" in response.json()["error"]["message"]


def test_limits_wrong_secrets_per_address(gateway: Gateway) -> None:
    secret = gateway.open()
    for _ in range(3):
        assert gateway.enroll("0" * 24).status_code == 401

    # Even the right one, from that address.
    assert gateway.enroll(secret).status_code == 429
    # Another address may still enroll.
    assert gateway.enroll(secret, peer=("192.168.1.21", 50000)).status_code == 200


def test_closes_the_window_after_ten_wrong_secrets(gateway: Gateway) -> None:
    secret = gateway.open()
    for attempt in range(MAX_FAILURES):
        assert gateway.enroll("0" * 24, peer=(f"192.168.1.{100 + attempt}", 50000)).status_code == 401

    response = gateway.enroll(secret)
    assert response.json()["error"]["code"] == "enrollment_closed"
    # A new window opens again.
    assert gateway.enroll(gateway.open()).status_code == 200


def test_refuses_a_bridge_id_that_isnt_the_bridge_at_that_address(gateway: Gateway) -> None:
    response = gateway.enroll(gateway.open(), bridge_id="9" * 32)

    assert (response.status_code, response.json()["error"]["code"]) == (422, "bridge_mismatch")
    assert gateway.hosts() == []


def test_refuses_a_bridge_without_info_or_that_is_down(gateway: Gateway) -> None:
    secret = gateway.open()
    gateway.bridges[URL] = httpx.Response(404, json={"detail": "Not Found"})
    assert gateway.enroll(secret).json()["error"]["code"] == "bridge_mismatch"

    del gateway.bridges[URL]
    response = gateway.enroll(secret)
    assert (response.status_code, response.json()["error"]["code"]) == (502, "bridge_unreachable")


@pytest.mark.parametrize(
    "url",
    ["http://8.8.8.8:8765", "http://100.64.0.1:8765", "http://agents.example.com:8765", "http://0.0.0.0:8765"],
)
def test_refuses_a_url_off_the_lan(gateway: Gateway, url: str) -> None:
    gateway.bridges[url] = httpx.Response(200, json=INFO)
    response = gateway.enroll(gateway.open(), url=url)

    assert response.status_code == 422
    assert response.json()["error"]["code"] in ("url_not_private", "bad_url")
    assert gateway.asked == []


@pytest.mark.parametrize("header", ["CF-Connecting-IP", "CF-Ray"])
def test_refuses_what_the_tunnel_forwarded(gateway: Gateway, header: str) -> None:
    # cloudflared calls from loopback, which is otherwise allowed.
    response = gateway.enroll(gateway.open(), peer=("127.0.0.1", 50000), headers={header: "1.2.3.4"})

    assert response.status_code == 404
    assert gateway.hosts() == []


def test_refuses_callers_off_the_lan(gateway: Gateway) -> None:
    response = gateway.enroll(gateway.open(), peer=("8.8.8.8", 50000))

    assert response.status_code == 404
    assert gateway.hosts() == []


def test_re_enrolling_replaces_the_record_in_place(gateway: Gateway) -> None:
    secret = gateway.open()
    gateway.bridges["http://192.168.1.21:8765"] = httpx.Response(200, json={**INFO, "bridge_id": "2" * 32})
    gateway.enroll(secret)
    gateway.enroll(secret, bridge_id="2" * 32, url="http://192.168.1.21:8765", name="linux")
    gateway.bridges["http://192.168.1.22:9000"] = httpx.Response(200, json=INFO)

    response = gateway.enroll(secret, url="http://192.168.1.22:9000", token="c" * 32, name="studio")

    assert response.json()["name"] == "studio"
    hosts = gateway.hosts()
    assert [(h["name"], h["url"], h["token"]) for h in hosts] == [
        ("studio", "http://192.168.1.22:9000", "c" * 32),
        ("linux", "http://192.168.1.21:8765", TOKEN),
    ]


def test_a_bridge_now_at_an_old_records_address_replaces_it(gateway: Gateway) -> None:
    # E.g. added by hand with `bridges add`, or enrolled before a reinstall.
    Registry(gateway.state_dir / "bridges.json").add("mac", URL, "a" * 32, {"api_version": 2})

    response = gateway.enroll(gateway.open())

    assert response.status_code == 200, response.text
    [host] = gateway.hosts()
    assert (host["bridge_id"], host["token"]) == (BRIDGE_ID, TOKEN)


def test_refuses_a_name_another_bridge_has(gateway: Gateway) -> None:
    Registry(gateway.state_dir / "bridges.json").add("mac", "http://192.168.1.50:8765", "a" * 32, None)

    response = gateway.enroll(gateway.open())

    assert (response.status_code, response.json()["error"]["code"]) == (409, "name_taken")
    assert [host["url"] for host in gateway.hosts()] == ["http://192.168.1.50:8765"]


@pytest.mark.parametrize(("field", "value"), [("name", "Studio Mac"), ("bridge_id", "../../x"), ("token", "short")])
def test_refuses_fields_that_wont_do(gateway: Gateway, field: str, value: str) -> None:
    response = gateway.enroll(gateway.open(), **{field: value})

    assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_request")
    assert gateway.asked == []


def test_refuses_an_oversized_request(gateway: Gateway) -> None:
    response = gateway.enroll(gateway.open(), name="x" * 20_000)

    assert response.status_code == 413


def address(gateway: Gateway, url: str, token: str = TOKEN, bridge_id: str = BRIDGE_ID, peer=LAN) -> httpx.Response:
    return gateway.client(peer).put(f"/bridges/{bridge_id}/address", json={"url": url},
                                    headers={"Authorization": f"Bearer {token}"})


def test_a_bridge_updates_its_address_with_its_token(gateway: Gateway) -> None:
    gateway.enroll(gateway.open())
    gateway.bridges["http://192.168.1.20:9999"] = httpx.Response(200, json=INFO)

    response = address(gateway, "http://192.168.1.20:9999")

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "mac"
    assert gateway.hosts()[0]["url"] == "http://192.168.1.20:9999"
    # The same address again needs no check.
    del gateway.bridges["http://192.168.1.20:9999"]
    assert address(gateway, "http://192.168.1.20:9999").status_code == 200


def test_a_new_address_must_answer_as_the_bridge(gateway: Gateway) -> None:
    gateway.enroll(gateway.open())
    gateway.bridges["http://127.0.0.1:2000"] = httpx.Response(500, json={"detail": "private metrics"})
    gateway.bridges["http://127.0.0.1:2001"] = httpx.Response(200, json={**INFO, "bridge_id": "9" * 32})

    refused = address(gateway, "http://127.0.0.1:2000")
    other = address(gateway, "http://127.0.0.1:2001")
    down = address(gateway, "http://127.0.0.1:2002")

    assert (refused.status_code, other.status_code, down.status_code) == (502, 422, 502)
    # Nothing of what the address answered comes back.
    assert "private metrics" not in refused.text
    assert gateway.hosts()[0]["url"] == URL


def test_a_bridge_now_at_another_records_address_takes_it(gateway: Gateway) -> None:
    # E.g. two hosts that swapped addresses: the one still holding it is out of date.
    secret = gateway.open()
    other = "http://192.168.1.21:8765"
    gateway.bridges[other] = httpx.Response(200, json={**INFO, "bridge_id": "2" * 32})
    gateway.enroll(secret)
    gateway.enroll(secret, bridge_id="2" * 32, url=other, name="linux")
    gateway.bridges[other] = httpx.Response(200, json=INFO)

    response = address(gateway, other)

    assert response.status_code == 200, response.text
    assert [(h["name"], h["url"]) for h in gateway.hosts()] == [("mac", other)]


def test_address_updates_are_refused_without_the_token_or_off_the_lan(gateway: Gateway) -> None:
    gateway.enroll(gateway.open())
    gateway.bridges["http://192.168.1.20:9999"] = httpx.Response(200, json=INFO)

    assert address(gateway, "http://192.168.1.20:9999", token="x" * 32).status_code == 401
    assert address(gateway, "http://192.168.1.20:9999", bridge_id="9" * 32).status_code == 404
    assert address(gateway, "http://8.8.8.8:8765").status_code == 422
    assert address(gateway, "http://192.168.1.20:9999", peer=("8.8.8.8", 1)).status_code == 404
    assert gateway.hosts()[0]["url"] == URL


def test_a_bridge_removes_itself_with_its_token(gateway: Gateway) -> None:
    gateway.enroll(gateway.open())
    client = gateway.client()
    wrong = client.delete(f"/bridges/{BRIDGE_ID}", headers={"Authorization": f"Bearer {'x' * 32}"})
    right = client.delete(f"/bridges/{BRIDGE_ID}", headers={"Authorization": f"Bearer {TOKEN}"})
    again = client.delete(f"/bridges/{BRIDGE_ID}", headers={"Authorization": f"Bearer {TOKEN}"})

    assert (wrong.status_code, right.status_code, again.status_code) == (401, 204, 404)
    assert gateway.hosts() == []


def test_other_paths_are_for_cloudflared_on_this_host_only(gateway: Gateway) -> None:
    local = gateway.client(("127.0.0.1", 50000))
    assert local.get("/healthz").text == "ok"
    assert local.post("/mcp").status_code == 401

    lan = gateway.client()
    assert lan.get("/healthz").status_code == 404
    assert lan.post("/mcp").status_code == 404
    assert lan.get("/.well-known/oauth-protected-resource/mcp").status_code == 404


def test_says_once_when_uvicorn_gave_the_tunnels_caller(
    gateway: Gateway, caplog: pytest.LogCaptureFixture
) -> None:
    # Started without --no-proxy-headers: uvicorn put X-Forwarded-For in the client.
    forwarded = gateway.client(("203.0.113.9", 0))
    for _ in range(2):
        assert forwarded.post("/mcp", headers={"CF-Connecting-IP": "203.0.113.9"}).status_code == 404
    # Not for callers on the LAN, who can send any header.
    gateway.client().post("/mcp", headers={"CF-Connecting-IP": "203.0.113.9"})

    errors = [r.message for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "--no-proxy-headers" in errors[0]


def test_a_host_removed_by_hand_is_refused_at_once(gateway: Gateway) -> None:
    gateway.enroll(gateway.open())
    # In another process, as `sudo mcp-gateway bridges remove mac` runs.
    Registry(gateway.state_dir / "bridges.json").remove("mac")
    gateway.bridges["http://192.168.1.20:9999"] = httpx.Response(200, json=INFO)
    asked = len(gateway.asked)

    assert address(gateway, "http://192.168.1.20:9999").status_code == 404
    assert len(gateway.asked) == asked


@pytest.mark.parametrize(
    ("address", "lan"),
    [("10.1.2.3", True), ("172.16.0.1", True), ("192.168.0.1", True), ("127.0.0.1", True), ("169.254.1.1", True),
     ("::1", True), ("fe80::1", True), ("fd00::1", True), ("::ffff:192.168.1.2", True), ("8.8.8.8", False),
     ("100.64.0.1", False), ("0.0.0.0", False), ("2001:db8::1", False), ("testclient", False)],
)
def test_what_counts_as_the_lan(address: str, lan: bool) -> None:
    assert is_lan(address) is lan


def test_the_window_keeps_the_gateway_id(tmp_path: Path) -> None:
    enrollment = Enrollment(tmp_path / "enrollment.json")
    gateway_id = enrollment.gateway_id()
    enrollment.open()
    enrollment.open()

    assert Enrollment(tmp_path / "enrollment.json").gateway_id() == gateway_id
    assert oct((tmp_path / "enrollment.json").stat().st_mode & 0o777) == "0o600"


def test_an_unreadable_file_starts_over_once(tmp_path: Path) -> None:
    (tmp_path / "enrollment.json").write_text("{broken")
    enrollment = Enrollment(tmp_path / "enrollment.json")

    gateway_id = enrollment.gateway_id()

    assert enrollment.gateway_id() == gateway_id
    assert json.loads((tmp_path / "enrollment.json").read_text())["secret_sha256"] is None


def run_cli(tmp_path: Path, capsys: pytest.CaptureFixture, *args: str, lan: str | None = "192.168.1.10"):
    code = cli.main(["--state-dir", str(tmp_path), "enroll", *args], lan_address=lambda: lan)
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_opens_a_window_and_prints_the_string(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code, out, _ = run_cli(tmp_path, capsys)

    assert code == 0
    [string] = [word.strip("'") for word in out.split() if word.startswith("'crm1@")]
    prefix, secret = string.split("#")
    assert prefix == "crm1@192.168.1.10:8766"
    assert len(secret) == 24
    # The secret is kept only as a hash.
    assert secret not in (tmp_path / "enrollment.json").read_text()
    Enrollment(tmp_path / "enrollment.json").check(secret, "192.168.1.20")


def test_cli_takes_an_address_and_port(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code, out, _ = run_cli(tmp_path, capsys, "--address", "fd00::5", "--port", "9000", lan=None)

    assert code == 0
    assert "'crm1@[fd00::5]:9000#" in out


def test_cli_refuses_without_a_lan_address(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert run_cli(tmp_path, capsys, lan=None)[0] == 1
    code, _, err = run_cli(tmp_path, capsys, "--address", "8.8.8.8")
    assert code == 1
    assert "8.8.8.8 isn't a private" in err
