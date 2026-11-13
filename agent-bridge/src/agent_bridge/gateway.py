"""Client for the gateway's enrollment routes (mcp-gateway's enrollment.py), and the
address report a running bridge sends on every start."""

import asyncio
import logging
import re
from pathlib import Path

import httpx

from agent_bridge.config import Settings
from agent_bridge.state import State, StateError, rename

log = logging.getLogger(__name__)

# What `sudo mcp-gateway enroll` prints: crm1@<address>:<port>#<secret>.
ENROLLMENT = re.compile(r"crm1@(\[[0-9A-Fa-f:.]+\]|[0-9.]+):([0-9]{1,5})#([0-9a-f]{24})")
# Enrolling waits for the gateway to ask this bridge's /info.
TIMEOUT = httpx.Timeout(20, connect=5)
# Seconds before each try of the address report: the first may come before this bridge
# listens, or while the gateway is still starting.
REPORT_DELAYS = (1, 2, 4, 8, 16)


class GatewayError(Exception):
    """The gateway refused, with its message and status."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class GatewayUnreachable(GatewayError):
    pass


def parse_enrollment(text: str) -> tuple[str, str]:
    """The gateway's URL and the secret in an enrollment string."""
    match = ENROLLMENT.fullmatch(text.strip())
    if match is None:
        raise ValueError(
            "That isn't an enrollment string. Run `sudo mcp-gateway enroll` on the gateway host; "
            "it prints one like crm1@192.168.1.10:8766#…"
        )
    host, port, secret = match.groups()
    return f"http://{host}:{port}", secret


async def enroll(gateway_url: str, secret: str, state: State, settings: Settings) -> dict:
    body = {"secret": secret, "bridge_id": state.bridge_id, "url": settings.url, "token": settings.token,
            "name": settings.name}
    return _joined(gateway_url, await _request("POST", gateway_url, "/enroll", json=body))


async def report_address(gateway_url: str, state: State, settings: Settings) -> dict:
    reply = await _request("PUT", gateway_url, f"/bridges/{state.bridge_id}/address", token=settings.token,
                           json={"url": settings.url})
    return _joined(gateway_url, reply)


def _joined(base: str, reply) -> dict:
    """The gateway's {name, gateway_id, api_version}; anything else isn't a gateway's answer."""
    if not (isinstance(reply, dict) and isinstance(reply.get("name"), str)
            and isinstance(reply.get("gateway_id"), str) and isinstance(reply.get("api_version", 0), int)):
        raise GatewayError(f"{base} answered, but not as an mcp-gateway")
    return reply


async def unenroll(gateway_url: str, state: State, token: str) -> None:
    await _request("DELETE", gateway_url, f"/bridges/{state.bridge_id}", token=token)


async def keep_address(state: State, settings: Settings, path: Path | None = None) -> None:
    """Tells the gateway this bridge's address, and saves the name it answers with to the
    state file at `path`; on failure, logs how to enroll again."""
    gateway = state.gateway
    assert gateway is not None
    for attempt, delay in enumerate(REPORT_DELAYS):
        await asyncio.sleep(delay)
        try:
            reply = await report_address(gateway.url, state, settings)
        except GatewayError as error:
            # Not enrolled any more, or a token the gateway doesn't know: trying again won't help.
            if error.status in (401, 404) or attempt == len(REPORT_DELAYS) - 1:
                log.warning(
                    "Couldn't tell the gateway at %s this host's address (%s). agent-bridge works on, but "
                    "the gateway may not reach it. To enroll again, run `sudo mcp-gateway enroll` on the "
                    "gateway host, then `agent-bridge enroll <string>` here.",
                    gateway.url, error,
                )
                return
            continue
        name = reply["name"]
        log.info("The gateway at %s has this host as %s, at %s.", gateway.url, name, settings.url)
        if path is not None:
            try:
                rename(path, gateway, name)
            except StateError as error:
                log.warning("%s", error)
        return


async def _request(method: str, base: str, path: str, *, token: str | None = None, json: dict | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(base_url=base, timeout=TIMEOUT) as client:
            response = await client.request(method, path, json=json, headers=headers)
    except httpx.TransportError as error:
        raise GatewayUnreachable(f"no answer from the gateway at {base} ({type(error).__name__})") from None
    if response.is_success:
        return response.json() if response.content else None
    try:
        problem = response.json()["error"]
        message, code = str(problem["message"]), str(problem["code"])
    except (ValueError, KeyError, TypeError):
        message, code = f"the gateway at {base} answered HTTP {response.status_code}", None
    raise GatewayError(message, response.status_code, code)
