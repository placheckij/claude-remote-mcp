"""Client for the agent-bridge HTTP API over the LAN."""

from typing import Any

import httpx

from mcp_gateway.replies import invalid

# A sleeping agent host drops packets instead of refusing them, so connecting gets its own
# short timeout; otherwise a prompt's long read timeout would also apply to connecting and
# claude.ai would give up before the user hears that the host is down.
CONNECT_TIMEOUT_S = 5.0
DEFAULT_TIMEOUT_S = 30.0
# agent-bridge answers a wait right after its deadline; this covers reading the output.
# It is kept small because claude.ai gives up on the whole tool call after about 30 s in
# voice mode (see tools.MAX_WAIT_SECONDS).
WAIT_MARGIN_S = 5
# A start waits for the shell, for Claude Code and for 5 s of trust-dialog check, and can
# take longer than claude.ai listens; past this the tool says the agent may still be starting.
# It covers asking the host about itself first (tools.start_agent).
START_TIMEOUT_S = 25
# /info answers at once; it only runs `herdr --version`.
INFO_TIMEOUT_S = 5


class BridgeError(Exception):
    """A refusal or failure reported by agent-bridge, with its human-readable message and,
    if it answered, the HTTP status."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class BridgeTimeout(BridgeError):
    """agent-bridge took the request but didn't answer in time; it may have acted on it."""


class AgentBridge:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    @classmethod
    def connect(cls, url: str, token: str) -> "AgentBridge":
        return cls(
            httpx.AsyncClient(
                base_url=url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=httpx.Timeout(DEFAULT_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            )
        )

    async def _request(self, method: str, path: str, *, timeout: float | None = None, **kwargs) -> Any:
        try:
            response = await self.client.request(
                method,
                path,
                timeout=httpx.Timeout(timeout or DEFAULT_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                **kwargs,
            )
        except httpx.ReadTimeout as error:
            raise BridgeTimeout("The agent host didn't answer in time.") from error
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise BridgeError(
                f"The agent host is not reachable ({type(error).__name__}). It may be asleep or offline."
            ) from error
        except httpx.TransportError as error:
            raise BridgeError(f"The agent host is not reachable ({type(error).__name__}).") from error
        if response.is_success:
            return response.json() if response.content else None
        raise BridgeError(_message(response), response.status_code)

    async def info(self) -> dict | None:
        """What the agent host says about itself; None from an agent-bridge before /info."""
        try:
            return await self._request("GET", "/info", timeout=INFO_TIMEOUT_S)
        except BridgeError as error:
            if error.status == 404:
                return None
            raise

    async def list_agents(self) -> list[dict]:
        return await self._request("GET", "/agents")

    async def get_agent(self, agent_id: str) -> dict:
        return await self._request("GET", f"/agents/{agent_id}")

    async def read_output(self, agent_id: str, lines: int) -> dict:
        return await self._request("GET", f"/agents/{agent_id}/output", params={"lines": lines})

    async def prompt(self, agent_id: str, text: str, timeout_s: int, lines: int) -> dict:
        return await self._request(
            "POST",
            f"/agents/{agent_id}/prompt",
            json={"text": text, "timeout_s": timeout_s, "lines": lines},
            timeout=timeout_s + WAIT_MARGIN_S,
        )

    async def wait(self, agent_id: str, timeout_s: int, lines: int, expect_change: bool = False) -> dict:
        return await self._request(
            "POST",
            f"/agents/{agent_id}/wait",
            json={"timeout_s": timeout_s, "lines": lines, "expect_change": expect_change},
            timeout=timeout_s + WAIT_MARGIN_S,
        )

    async def send_keys(self, agent_id: str, keys: list[str]) -> None:
        await self._request("POST", f"/agents/{agent_id}/keys", json={"keys": keys})

    async def start_agent(
        self,
        *,
        project: str | None = None,
        cwd: str | None = None,
        name: str | None = None,
        account: str | None = None,
        timeout: float = START_TIMEOUT_S,
    ) -> dict:
        """Start in `project` (agent-bridge resolves it), or in `cwd` on an agent-bridge
        before /info. Without a name the agent is named after its folder."""
        fields = {"project": project, "cwd": cwd, "name": name, "account": account}
        body = {key: value for key, value in fields.items() if value}
        # Starting waits for Claude to boot and for the trust check.
        return await self._request("POST", "/agents", json=body, timeout=timeout)

    async def rename_agent(self, agent_id: str, name: str) -> dict:
        return await self._request("PATCH", f"/agents/{agent_id}", json={"name": name})

    async def stop_agent(self, agent_id: str) -> None:
        await self._request("DELETE", f"/agents/{agent_id}")

    async def aclose(self) -> None:
        await self.client.aclose()


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"agent-bridge returned HTTP {response.status_code}."
    if isinstance(body.get("error"), dict):
        return body["error"]["message"]
    detail = body.get("detail")
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail and all(isinstance(error, dict) for error in detail):
        return f"agent-bridge refused the request: {invalid(detail)}"
    return f"agent-bridge rejected the request (HTTP {response.status_code}): {detail}"
