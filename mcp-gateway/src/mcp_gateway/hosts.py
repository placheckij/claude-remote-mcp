"""The agent hosts behind the gateway: an agent-bridge client per registry record, the
agent ids that name them ("mac/w1:p1"), and which bridge versions the gateway calls."""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

from mcp.server.mcpserver.exceptions import ToolError

from mcp_gateway.bridge import AgentBridge, BridgeError
from mcp_gateway.registry import NAME_PATTERN, Bridge, Registry, RegistryError

log = logging.getLogger(__name__)

# The bridge API this gateway speaks; it also calls bridges one version behind, so the
# gateway and its hosts can be updated one after another. 1 is the API before /info.
API_VERSION = 2
OLDEST_API_VERSION = API_VERSION - 1
# What an agent-bridge from before /info is recorded as having said.
NO_INFO = {"api_version": 1}
# Used when the legacy bridge can't say its own name at the import.
LEGACY_NAME = "host"
NO_HOSTS = "No agent hosts are set up. Add one on the gateway host with `sudo mcp-gateway bridges add <url>`."


class Host:
    def __init__(self, record: Bridge, bridge: AgentBridge):
        self.record = record
        self.bridge = bridge

    @property
    def name(self) -> str:
        return self.record.name

    @property
    def api_version(self) -> int | None:
        """None until the bridge first answers."""
        return self.record.info["api_version"] if self.record.info else None


class Hosts:
    def __init__(
        self,
        registry: Registry,
        connect: Callable[[str, str], AgentBridge] = AgentBridge.connect,
        legacy: tuple[str, str] | None = None,
        on_change: Callable[[list[str]], None] | None = None,
    ):
        self.registry = registry
        self.connect = connect
        # MCP_GATEWAY_BRIDGE_URL and _TOKEN from before the registry, imported once.
        self.legacy = legacy
        self.on_change = on_change
        self._hosts: dict[str, Host] = {}
        # Clients of removed hosts, closed at shutdown: a request may still be using one.
        self._removed: list[AgentBridge] = []
        self._generation: int | None = None
        self._sync()

    async def refresh(self) -> None:
        """Takes in changes to the registry file; called before every MCP request."""
        # Taken before awaiting, so requests at the same time don't import it twice.
        legacy, self.legacy = self.legacy, None
        if legacy and not self.registry.bridges:
            await self._import_legacy(*legacy)
        self.registry.reload()
        self._sync()

    def all(self) -> list[Host]:
        # The registry may have taken in another process's change while saving its own.
        self._sync()
        return [self._host(record) for record in self.registry.bridges]

    def named(self, name: str) -> Host:
        for host in self.all():
            if host.name == name:
                return host
        if not self.registry.bridges:
            raise ToolError(NO_HOSTS)
        raise ToolError(f"No host named {name}; hosts: {', '.join(self.registry.names())}.")

    async def route(self, agent_id: str) -> tuple[Host, str]:
        """The host an agent id names and the id the bridge knows it by. A bare id is
        taken while one host is set up, as before there were several."""
        name, _, bare = agent_id.rpartition("/")
        hosts = self.all()
        if name:
            host = self.named(name)
        elif len(hosts) == 1:
            host = hosts[0]
        elif not hosts:
            raise ToolError(NO_HOSTS)
        else:
            raise ToolError(
                f'Several hosts are set up; give the agent id with its host, like "{hosts[0].name}/{bare}". '
                f"Hosts: {', '.join(host.name for host in hosts)}."
            )
        await self.usable(host)
        return host, bare

    def show(self, host: Host, agent_id: str) -> str:
        """An agent id as the user sees it: with its host only when there are several."""
        return f"{host.name}/{agent_id}" if len(self.registry.bridges) > 1 else agent_id

    async def info(self, host: Host) -> dict:
        """Asks the bridge about itself and records the answer."""
        info = await host.bridge.info() or NO_INFO
        self.registry.seen(host.record.bridge_id, info)
        host.record = self._record(host.record.bridge_id) or host.record
        return info

    async def usable(self, host: Host) -> None:
        """Refuses a host whose bridge version the gateway doesn't call. The bridge may
        have been updated since it last said, so it is asked again before refusing."""
        if version_problem(host.name, host.api_version) is None:
            return
        try:
            info = await self.info(host)
        except BridgeError as error:
            raise ToolError(str(error)) from error
        problem = version_problem(host.name, info["api_version"])
        if problem is not None:
            raise ToolError(problem)

    async def aclose(self) -> None:
        for bridge in [host.bridge for host in self._hosts.values()] + self._removed:
            await bridge.aclose()
        self._hosts, self._removed = {}, []

    # --- Clients -----------------------------------------------------------------------

    def _host(self, record: Bridge) -> Host:
        host = self._hosts[record.bridge_id]
        host.record = record
        return host

    def _record(self, bridge_id: str) -> Bridge | None:
        return next((record for record in self.registry.bridges if record.bridge_id == bridge_id), None)

    def _sync(self) -> None:
        """A client for every record, once the registry took anything from the file; a new
        one when its URL or token changed."""
        if self._generation == self.registry.generation:
            return
        self._generation = self.registry.generation
        wanted = {record.bridge_id: record for record in self.registry.bridges}
        for bridge_id, host in list(self._hosts.items()):
            record = wanted.get(bridge_id)
            if record is None or (record.url, record.token) != (host.record.url, host.record.token):
                self._removed.append(self._hosts.pop(bridge_id).bridge)
        for bridge_id, record in wanted.items():
            if bridge_id not in self._hosts:
                self._hosts[bridge_id] = Host(record, self.connect(record.url, record.token))
        if self.on_change:
            self.on_change(self.registry.names())

    async def _import_legacy(self, url: str, token: str) -> None:
        bridge = self.connect(url, token)
        try:
            info = await bridge.info() or NO_INFO
        except BridgeError as error:
            # Imported anyway: the host may only be asleep, and nothing would work without it.
            log.warning("The agent host at %s didn't answer at the import (%s).", url, error)
            info = None
        finally:
            await bridge.aclose()
        name = (info or {}).get("name")
        if not isinstance(name, str) or not re.fullmatch(NAME_PATTERN, name):
            name = LEGACY_NAME
        try:
            self.registry.add(name, url, token, info)
        except (OSError, RegistryError) as error:
            log.error("Couldn't import MCP_GATEWAY_BRIDGE_URL into %s (%s).", self.registry.path, error)
            return
        log.warning(
            "Imported the agent host at %s as %s into %s; MCP_GATEWAY_BRIDGE_URL and "
            "MCP_GATEWAY_BRIDGE_TOKEN are no longer needed.",
            url, name, self.registry.path,
        )


def version_problem(name: str, api_version: int | None) -> str | None:
    """Why the gateway won't call a bridge at this API version, or None if it will. An
    unknown version (never answered) is tried."""
    if api_version is None or OLDEST_API_VERSION <= api_version <= API_VERSION:
        return None
    if api_version < OLDEST_API_VERSION:
        return f"{name} needs an update: its agent-bridge is too old for this gateway."
    return f"{name} needs an update: its agent-bridge is newer than this gateway, so update the gateway."


async def gather_hosts(hosts: list[Host], ask: Callable[[Host], Awaitable], timeout: float) -> list:
    """`ask` for every host at once, each within `timeout`; a BridgeError in place of an
    answer that failed or came too late."""

    async def one(host: Host):
        try:
            return await asyncio.wait_for(ask(host), timeout)
        except TimeoutError:
            return BridgeError(f"It didn't answer within {timeout:g} s.")
        except BridgeError as error:
            return error

    return await asyncio.gather(*(one(host) for host in hosts))
