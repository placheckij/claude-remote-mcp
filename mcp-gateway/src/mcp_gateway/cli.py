"""`mcp-gateway bridges …`: adds, lists, renames and removes agent hosts in bridges.json.

The running gateway takes changes in before its next request. On the gateway host the
installed wrapper runs this as the service user with the state folder set (setup.sh).
"""

import argparse
import asyncio
import getpass
import os
import re
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from mcp_gateway.bridge import AgentBridge, BridgeError
from mcp_gateway.config import ENV_FILE
from mcp_gateway.hosts import version_problem
from mcp_gateway.registry import NAME_PATTERN, Registry, RegistryError

EXAMPLE_URL = "http://192.168.1.20:8765"


class _StateDir(BaseSettings):
    """Only the state folder, from where the gateway reads it."""

    model_config = SettingsConfigDict(env_prefix="MCP_GATEWAY_", extra="ignore", env_ignore_empty=True)

    state_dir: Path


class _Refused(Exception):
    """What to print before exiting with 1."""


def main(
    argv: list[str] | None = None,
    *,
    ask_token: Callable[[str], str] = getpass.getpass,
    connect: Callable[[str, str], AgentBridge] = AgentBridge.connect,
) -> int:
    args = _parser().parse_args(argv)
    try:
        state_dir = _state_dir(args.state_dir)
        _become_owner_of(state_dir)
        registry = Registry(state_dir / "bridges.json")
        if args.command == "add":
            _add(registry, args.url, args.name, ask_token, connect)
        elif args.command == "list":
            _list(registry)
        elif args.command == "rename":
            registry.rename(args.name, args.new_name)
            print(f"Renamed {args.name} to {args.new_name}.")
        else:
            registry.remove(args.name)
            print(f"Removed {args.name}.")
    except (_Refused, RegistryError) as error:
        print(error, file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Can't change {error.filename or 'the agent hosts'} ({error.strerror}).", file=sys.stderr)
        return 1
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-gateway", description="Manage the gateway's agent hosts.")
    parser.add_argument("--state-dir", type=Path, help="default: MCP_GATEWAY_STATE_DIR, also from .env")
    groups = parser.add_subparsers(dest="group", required=True)
    bridges = groups.add_parser("bridges", help="the agent-bridges this gateway calls").add_subparsers(
        dest="command", required=True
    )
    add = bridges.add_parser("add", help="add an agent-bridge; its token is asked for")
    add.add_argument("url", help=f"its address on the LAN, e.g. {EXAMPLE_URL}")
    add.add_argument("--name", help="host name in agent ids; default: the name the bridge gives")
    bridges.add_parser("list", help="list the agent hosts")
    rename = bridges.add_parser("rename", help="rename an agent host")
    rename.add_argument("name")
    rename.add_argument("new_name")
    remove = bridges.add_parser("remove", help="remove an agent host")
    remove.add_argument("name")
    return parser


def _state_dir(given: Path | None) -> Path:
    if given is not None:
        return given
    try:
        return _StateDir(_env_file=ENV_FILE).state_dir
    except ValidationError:
        raise _Refused("Set MCP_GATEWAY_STATE_DIR or pass --state-dir.") from None


def _become_owner_of(state_dir: Path) -> None:
    """Run as root (`sudo mcp-gateway bridges …` without the wrapper), drops to the user
    who owns the state folder before touching anything in it: files root made there would
    be unreadable for the gateway, and that user could plant links for root to follow."""
    if os.name != "posix" or os.geteuid() != 0:
        return
    try:
        owner = state_dir.stat()
    except FileNotFoundError:
        raise _Refused(f"{state_dir} doesn't exist; start mcp-gateway once to create it.") from None
    if owner.st_uid == 0:
        return
    os.setgroups([])
    os.setgid(owner.st_gid)
    os.setuid(owner.st_uid)


def _add(
    registry: Registry,
    url: str,
    name: str | None,
    ask_token: Callable[[str], str],
    connect: Callable[[str, str], AgentBridge],
) -> None:
    url = url.rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path or parts.query:
        raise _Refused(f"Give the agent-bridge's address, like {EXAMPLE_URL}.")
    try:
        token = ask_token("agent-bridge token (AGENT_BRIDGE_TOKEN on the agent host): ").strip()
    except (EOFError, KeyboardInterrupt):
        # No terminal to ask on (stdin closed), Ctrl-D or Ctrl-C.
        token = ""
    if not token:
        raise _Refused("The token is needed: AGENT_BRIDGE_TOKEN in the agent host's settings.")
    info = asyncio.run(_info(connect(url, token)))
    if info is None:
        raise _Refused(f"The agent-bridge at {url} has no /info: update agent-bridge on it first.")
    if name is None:
        name = info.get("name")
        # The bridge names itself after its hostname, which may not fit a host name here.
        if not isinstance(name, str) or not re.fullmatch(NAME_PATTERN, name):
            raise _Refused(
                f"The agent-bridge calls itself {name!r}, which can't name a host here. "
                "Give it one with --name, like --name mac."
            )
    registry.add(name, url, token, info)
    print(f"Added {name} ({info.get('platform')}, agent-bridge {info.get('bridge_version')}) at {url}.")
    if problem := version_problem(name, info.get("api_version")):
        print(f"{problem} Until then it shows in list_agents, but isn't called.", file=sys.stderr)


async def _info(bridge: AgentBridge) -> dict | None:
    try:
        return await bridge.info()
    except BridgeError as error:
        raise _Refused(f"Couldn't add it: {error}") from None
    finally:
        await bridge.aclose()


def _list(registry: Registry) -> None:
    if not registry.bridges:
        print("No agent hosts. Add one with: mcp-gateway bridges add <url>")
        return
    names = max(len(bridge.name) for bridge in registry.bridges)
    urls = max(len(bridge.url) for bridge in registry.bridges)
    for bridge in registry.bridges:
        if bridge.info is None or bridge.last_seen is None:
            about = "never answered"
        else:
            seen = datetime.fromtimestamp(bridge.last_seen).strftime("%Y-%m-%d %H:%M")
            platform = bridge.info.get("platform", "agent-bridge before /info")
            about = f"{platform}, API {bridge.info['api_version']}, last seen {seen}"
        print(f"{bridge.name:<{names}}  {bridge.url:<{urls}}  {about}")
