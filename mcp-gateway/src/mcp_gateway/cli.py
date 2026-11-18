"""`mcp-gateway bridges …` adds, lists, renames and removes agent hosts in bridges.json;
`mcp-gateway enroll` opens a window for agent-bridges to add themselves (enrollment.py);
`mcp-gateway setup` installs this version as the service (deploy/setup.sh).

The running gateway takes changes in before its next request. On the gateway host the
installed wrapper runs this as the service user with the state folder set (setup.sh).
"""

import argparse
import asyncio
import getpass
import json
import os
import re
import socket
import subprocess
import sys
import sysconfig
from collections.abc import Callable
from datetime import datetime
from importlib.metadata import Distribution, distributions, version
from importlib.resources import as_file, files
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from mcp_gateway.bridge import AgentBridge, BridgeError
from mcp_gateway.config import ENV_FILE
from mcp_gateway.enrollment import PREFIX, WINDOW_S, Enrollment, is_lan
from mcp_gateway.hosts import version_problem
from mcp_gateway.registry import NAME_PATTERN, Registry, RegistryError

EXAMPLE_URL = "http://192.168.1.20:8765"
# Where the gateway listens (setup.sh's PORT).
DEFAULT_PORT = 8766
PACKAGE = "claude-remote-mcp-gateway"
# What setup.sh reads from its environment, which sudo would otherwise drop.
SETUP_VARIABLES = (
    "PUBLIC_HOST", "PROJECTS_ROOT", "TUNNEL_NAME", "PORT", "AGENT_BRIDGE_URL", "AGENT_BRIDGE_TOKEN",
    "MCP_GATEWAY_OAUTH_PASSPHRASE", "ROTATE_PASSPHRASE", "MCP_GATEWAY_TOKEN", "REMOVE_TOKEN",
)


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
    lan_address: Callable[[], str | None] = lambda: _lan_address(),
    reachable: Callable[[str, int], bool] = lambda address, port: _reachable(address, port),
    run: Callable[[list[str]], int] = subprocess.call,
    site_packages: Callable[[], list[str]] = lambda: _site_packages(),
) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.group == "setup":
            return _setup(run, site_packages())
        state_dir = _state_dir(args.state_dir)
        _become_owner_of(state_dir)
        if args.group == "enroll":
            _enroll(Enrollment(state_dir / "enrollment.json"), args.address or lan_address(), args.port, reachable)
            return 0
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
        print(f"Can't use {error.filename or 'the agent hosts'} ({error.strerror}).", file=sys.stderr)
        return 1
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-gateway", description="Set up the gateway and manage its agent hosts.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version(PACKAGE)}")
    parser.add_argument("--state-dir", type=Path, help="default: MCP_GATEWAY_STATE_DIR, also from .env")
    groups = parser.add_subparsers(dest="group", required=True)
    groups.add_parser(
        "setup", help="install or update the service, the tunnel and the settings on this Debian-based host, "
                      "with sudo; keeps what is installed"
    )
    enroll = groups.add_parser(
        "enroll", help=f"let agent-bridges add themselves for {WINDOW_S // 60} minutes; prints the string they need"
    )
    enroll.add_argument("--address", help="this host's address on the LAN; default: the one the default route uses")
    # setup.sh's wrapper sets MCP_GATEWAY_PORT to the port it serves on.
    port = int(os.environ.get("MCP_GATEWAY_PORT") or DEFAULT_PORT)
    enroll.add_argument("--port", type=int, default=port, help=f"the gateway's port (default {port})")
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


def _setup(run: Callable[[list[str]], int], site_packages: list[str]) -> int:
    """Runs the shipped setup.sh as root on this version: the Python version and the exact
    packages of this environment, which it installs for the service. The service can't use
    this environment itself, which is in the user's home folder (ProtectHome=yes)."""
    if not sys.platform.startswith("linux"):
        raise _Refused("mcp-gateway setup is for a Debian-based Linux with systemd; elsewhere, install it by hand "
                       "(see the README).")
    installed = list(distributions(path=site_packages))
    if not any(_name(dist) == PACKAGE for dist in installed):
        raise _Refused(f"No {PACKAGE} here to install for the service. Install it with: uv tool install {PACKAGE}")
    requirements = sorted(_requirement(dist) for dist in installed)
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    with as_file(files("mcp_gateway") / "deploy" / "setup.sh") as script:
        command = ["bash", str(script), python, *requirements]
        if os.geteuid() != 0:
            command = ["sudo", f"--preserve-env={','.join(SETUP_VARIABLES)}", *command]
        return run(command)


def _site_packages() -> list[str]:
    paths = sysconfig.get_paths()
    return list(dict.fromkeys((paths["purelib"], paths["platlib"])))


def _name(dist: Distribution) -> str:
    return re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower()


def _requirement(dist: Distribution) -> str:
    """What installs `dist` again from where it came from (PEP 610's direct_url.json)."""
    name = _name(dist)
    origin = dist.read_text("direct_url.json")
    if origin is None:
        return f"{name}=={dist.version}"
    origin = json.loads(origin)
    url = origin["url"]
    if origin.get("dir_info", {}).get("editable"):
        path = unquote(urlsplit(url).path)
        raise _Refused(f"{name} is installed editable, from {path}, where the service can't read it. "
                       f"Install it without --editable: uv tool install {path}")
    if vcs := origin.get("vcs_info"):
        url = f"{vcs['vcs']}+{url}@{vcs['commit_id']}"
    if subdirectory := origin.get("subdirectory"):
        url += f"#subdirectory={subdirectory}"
    return f"{name} @ {url}"


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


def _enroll(enrollment: Enrollment, address: str | None, port: int, reachable: Callable[[str, int], bool]) -> None:
    if address is None:
        raise _Refused("Couldn't tell this host's LAN address; give it with --address.")
    if not is_lan(address):
        raise _Refused(f"{address} isn't a private, loopback or link-local IP address; give one with --address.")
    if "%" in address:
        raise _Refused(f"{address} has a zone, which the enrollment string can't carry; give another with --address.")
    host =f"[{address}]" if ":" in address else address
    # E.g. an IPv6 address while the gateway listens on 0.0.0.0, or one listening on 127.0.0.1.
    if not reachable(address, port):
        raise _Refused(
            f"Nothing answers at {host}:{port}, so agent hosts couldn't enroll there. Is mcp-gateway running, "
            "listening on that address? Give another with --address and --port."
        )
    secret, expires_at = enrollment.open()
    until = datetime.fromtimestamp(expires_at).strftime("%H:%M")
    print(f"Agent hosts can enroll until {until}, as many as you like. On each one, run:")
    print(f"  agent-bridge enroll '{PREFIX}@{host}:{port}#{secret}'")


def _reachable(address: str, port: int) -> bool:
    try:
        socket.create_connection((address, port), timeout=3).close()
    except OSError:
        return False
    return True


def _lan_address() -> str | None:
    """The address of the interface the default route goes through."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            # Picks the route only: a UDP socket sends nothing until written to.
            probe.connect(("192.0.2.1", 9))
        except OSError:
            return None
        return probe.getsockname()[0]


def _add(
    registry: Registry,
    url: str,
    name: str | None,
    ask_token: Callable[[str], str],
    connect: Callable[[str, str], AgentBridge],
) -> None:
    url = url.rstrip("/")
    try:
        parts = urlsplit(url)
        parts.port  # parsed only when read; raises on e.g. ":87a5"
    except ValueError:
        parts = None
    if not parts or parts.scheme not in ("http", "https") or not parts.hostname or parts.path or parts.query:
        raise _Refused(f"Give the agent-bridge's address, like {EXAMPLE_URL}.")
    # From the environment for a run without a terminal, e.g. setup.sh with
    # sudo --preserve-env=AGENT_BRIDGE_TOKEN; never from the command line, where others see it.
    token = os.environ.get("AGENT_BRIDGE_TOKEN", "").strip()
    if not token:
        try:
            token = ask_token("agent-bridge token (AGENT_BRIDGE_TOKEN on the agent host): ").strip()
        except (EOFError, KeyboardInterrupt):
            # No terminal to ask on (stdin closed), Ctrl-D or Ctrl-C.
            token = ""
    if not token:
        raise _Refused("The token is needed: AGENT_BRIDGE_TOKEN in the agent host's settings.")
    # It goes in an HTTP header, which takes printable ASCII only; a typo shouldn't end in a traceback.
    if not (token.isascii() and token.isprintable()):
        raise _Refused("That isn't an agent-bridge token: it has characters other than printable ASCII.")
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
