"""Agent-bridges add themselves to bridges.json: enrollment and the routes for it.

`mcp-gateway enroll` (cli.py) opens a window: a secret, kept as a hash in enrollment.json,
good for 30 minutes and any number of bridges. A bridge sends it to POST /enroll with its
id, address, token and name; the gateway asks that address's /info whether it is that
bridge, and adds it. The bridge reports a new address on every start (PUT
/bridges/{id}/address) and removes itself on uninstall (DELETE /bridges/{id}), both with
its own token.

The routes share the listener with /mcp but serve the LAN only: the tunnel doesn't
publish them, and they answer 404 to anything it forwarded or that comes from elsewhere.
"""

import hashlib
import hmac
import ipaddress
import logging
import secrets
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Router
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_gateway.bridge import AgentBridge, BridgeError
from mcp_gateway.files import locked, read_bytes, write_json
from mcp_gateway.hosts import API_VERSION
from mcp_gateway.registry import NameTaken, Registry, RegistryError, UnknownBridge, check_name_format, check_token
from mcp_gateway.replies import invalid

log = logging.getLogger(__name__)

# What `mcp-gateway enroll` prints is "crm1@<address>:<port>#<secret>".
PREFIX = "crm1"
WINDOW_S = 30 * 60
SECRET_BYTES = 12  # 96 bits
# Wrong secrets in one window: from one address, then from all, which closes it.
MAX_FAILURES_PER_ADDRESS = 3
MAX_FAILURES = 10
# Cloudflare adds both to every request the tunnel forwards.
TUNNEL_HEADERS = (b"cf-connecting-ip", b"cf-ray")
_PRIVATE = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))


class Refused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


class _Window(BaseModel):
    version: int = 1
    # Told to every bridge that enrolls, so it knows which gateway it belongs to.
    gateway_id: str
    secret_sha256: str | None = None
    expires_at: int = 0
    failures: int = 0


class Enrollment:
    """enrollment.json: the gateway's id and the open window, if any."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self._lock_path = path.with_name("enrollment.lock")
        # Wrong secrets per (window, address); a new window starts over.
        self._failures: dict[tuple[str, str], int] = {}

    def gateway_id(self) -> str:
        with locked(self._lock_path):
            window = self._read()
            if not self.path.exists():
                write_json(self.path, window.model_dump())
            return window.gateway_id

    def open(self) -> tuple[str, int]:
        """A new window, closing any open one; its secret and when it ends."""
        secret = secrets.token_hex(SECRET_BYTES)
        with locked(self._lock_path):
            window = self._read()
            window.secret_sha256 = _hash(secret)
            window.expires_at = int(self.clock()) + WINDOW_S
            window.failures = 0
            write_json(self.path, window.model_dump())
        return secret, window.expires_at

    def check(self, secret: str, address: str) -> None:
        """Refuses a secret that isn't the open window's, counting it against `address`."""
        with locked(self._lock_path):
            window = self._read()
            if window.secret_sha256 is None or self.clock() >= window.expires_at:
                raise Refused(
                    401, "enrollment_closed",
                    "No enrollment is open on the gateway. Run `sudo mcp-gateway enroll` there for a new string.",
                )
            key = (window.secret_sha256, address)
            if self._failures.get(key, 0) >= MAX_FAILURES_PER_ADDRESS:
                raise Refused(429, "too_many_attempts", "Too many wrong enrollment strings from this address.")
            if hmac.compare_digest(_hash(secret), window.secret_sha256):
                return
            self._failures[key] = self._failures.get(key, 0) + 1
            window.failures += 1
            if window.failures >= MAX_FAILURES:
                window.secret_sha256 = None
                log.warning("Closed the enrollment window after %d wrong strings.", window.failures)
            write_json(self.path, window.model_dump())
        log.warning("Enrollment refused for %s: wrong string.", address)
        raise Refused(401, "wrong_secret", "That isn't the gateway's current enrollment string.")

    def _read(self) -> _Window:
        try:
            return _Window.model_validate_json(read_bytes(self.path))
        except FileNotFoundError:
            return _Window(gateway_id=uuid.uuid4().hex)
        except ValidationError:
            # Unreadable: start over with no window open, as if it had expired. The gateway
            # gets a new id, which bridges only show.
            log.error("Can't read %s; starting it over.", self.path)
            return _Window(gateway_id=uuid.uuid4().hex)


class _EnrollRequest(BaseModel):
    secret: str
    bridge_id: str
    url: str
    token: str
    name: str


class _AddressRequest(BaseModel):
    url: str


def enrollment_routes(
    app: ASGIApp,
    registry: Registry,
    enrollment: Enrollment,
    connect: Callable[[str, str], AgentBridge],
) -> ASGIApp:
    """`app` with the enrollment routes in front of it."""

    async def enroll(request: Request) -> Response:
        body = await _body(request, _EnrollRequest)
        address = _peer(request)
        enrollment.check(body.secret, address)
        url = lan_url(body.url)
        _check(check_name_format, body.name, "name")
        _check(check_token, body.token, "token")
        bridge = connect(url, body.token)
        try:
            info = await bridge.info()
        except BridgeError as error:
            raise Refused(502, "bridge_unreachable", f"The gateway couldn't ask {url} about itself: {error}") from None
        finally:
            await bridge.aclose()
        if info is None or info.get("bridge_id") != body.bridge_id:
            raise Refused(
                422, "bridge_mismatch",
                f"{url} isn't this agent-bridge: is it running, with these settings, at that address?",
            )
        try:
            record = registry.enroll(body.bridge_id, body.name, url, body.token, info)
        except NameTaken as error:
            raise Refused(409, "name_taken", f"{error} Choose another name.") from None
        log.warning(
            "Enrolled %s at %s (%s, agent-bridge %s), from %s.",
            record.name, url, info.get("platform"), info.get("bridge_version"), address,
        )
        return _joined(record.name)

    async def update_address(request: Request) -> Response:
        bridge_id = _authorized(request)
        url = lan_url((await _body(request, _AddressRequest)).url)
        before = registry.find_id(bridge_id)
        record = registry.move(bridge_id, url)
        if before is not None and before.url != record.url:
            log.info("%s moved from %s to %s.", record.name, before.url, record.url)
        return _joined(record.name)

    async def unenroll(request: Request) -> Response:
        record = registry.forget(_authorized(request))
        log.warning("%s at %s removed itself.", record.name, record.url)
        return Response(status_code=204)

    def _authorized(request: Request) -> str:
        """The bridge_id in the path, if the request has that bridge's token."""
        bridge_id = request.path_params["bridge_id"]
        record = registry.find_id(bridge_id)
        if record is None:
            raise Refused(404, "not_enrolled", "This agent host isn't enrolled with the gateway.")
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), record.token.encode()):
            raise Refused(401, "unauthorized", "That isn't this agent host's token.")
        return bridge_id

    def _joined(name: str) -> Response:
        return JSONResponse({"name": name, "gateway_id": enrollment.gateway_id(), "api_version": API_VERSION})

    def handled(endpoint):
        async def handle(request: Request) -> Response:
            if (reason := _outside(request)) is not None:
                log.warning("404 %s %s: %s.", request.method, request.url.path, reason)
                return JSONResponse({"error": "not found"}, status_code=404)
            try:
                return await endpoint(request)
            except Refused as error:
                return _error(error.status, error.code, str(error))
            except UnknownBridge as error:
                return _error(404, "not_enrolled", str(error))
            except RegistryError as error:
                return _error(409, "conflict", str(error))
            except OSError as error:
                log.error("Can't save the agent hosts (%s).", error)
                return _error(500, "state_unwritable", "The gateway couldn't save its agent hosts; see its log.")

        return handle

    router = Router(
        routes=[
            Route("/enroll", handled(enroll), methods=["POST"]),
            Route("/bridges/{bridge_id}/address", handled(update_address), methods=["PUT"]),
            Route("/bridges/{bridge_id}", handled(unenroll), methods=["DELETE"]),
        ]
    )

    async def routes(scope: Scope, receive: Receive, send: Send) -> None:
        # Only these paths: a Router would also take the lifespan events, which `app` needs.
        if scope["type"] == "http" and (scope["path"] == "/enroll" or scope["path"].startswith("/bridges/")):
            await router(scope, receive, send)
        else:
            await app(scope, receive, send)

    return routes


def is_lan(address: str) -> bool:
    """A private, loopback or link-local address."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local or any(ip in network for network in _PRIVATE)


def lan_url(value: str) -> str:
    """A bridge's address, refused unless it is an IP address on the LAN: the gateway calls
    it, and mustn't be made to call anything else."""
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        url = None
    if url is None or url.scheme != "http" or not url.host or url.path not in ("", "/") or url.query:
        raise Refused(422, "bad_url", "The address must be like http://192.168.1.20:8765.")
    if not is_lan(url.host):
        raise Refused(
            422, "url_not_private",
            f"{url.host} isn't a private, loopback or link-local IP address; the gateway calls only those.",
        )
    return value.rstrip("/")


def _outside(request: Request) -> str | None:
    """Why a caller may not use the enrollment routes, or None if it may."""
    if any(name in TUNNEL_HEADERS for name, _ in request.scope["headers"]):
        return "forwarded by the tunnel"
    if not is_lan(_peer(request)):
        return f"{_peer(request)} isn't on the LAN"
    return None


def _peer(request: Request) -> str:
    return request.client.host if request.client else "?"


async def _body(request: Request, model: type[BaseModel]):
    try:
        return model.model_validate_json(await request.body())
    except ValidationError as error:
        # Without the input: it may hold the secret or the token.
        problems = error.errors(include_input=False, include_url=False)
        raise Refused(422, "invalid_request", f"Invalid request: {invalid(problems)}") from None


def _check(check: Callable[[str], str], value: str, field: str) -> None:
    try:
        check(value)
    except ValueError as error:
        raise Refused(422, "invalid_request", f"The {field} {error}.") from None


def _error(status: int, code: str, message: str) -> Response:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()
