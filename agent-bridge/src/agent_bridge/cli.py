"""`agent-bridge serve | enroll | unenroll | check`: what the platform setups run.

Exit codes are part of the deploy contract the setups are built against: `enroll` exits 3
when the name is taken (ask for another) and 4 when the gateway can't be reached;
`check --token-format` and `--name-format` exit 2 for a value that won't do.
"""

import argparse
import asyncio
import logging
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import uvicorn

from agent_bridge.app import create_app
from agent_bridge.config import Settings, check_name_format, check_token_format
from agent_bridge.gateway import GatewayError, GatewayUnreachable, enroll, parse_enrollment, report_address, unenroll
from agent_bridge.herdr import Herdr, HerdrError
from agent_bridge.host import API_VERSION
from agent_bridge.state import Gateway, StateError, env_file, load, load_or_create, rename, save, state_file

OK, FAILED, INVALID, NAME_TAKEN, UNREACHABLE = 0, 1, 2, 3, 4


class _Refused(Exception):
    """What to print before exiting with 1."""


def main(argv: list[str] | None = None, *, run: Callable[..., None] = uvicorn.run) -> int:
    """`run` serves the app; tests replace it."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "serve":
            return _serve(run)
        if args.command == "enroll":
            return _enroll(args.string)
        if args.command == "unenroll":
            return _unenroll()
        if args.token_format is not None:
            return _format(check_token_format, args.token_format, "The token")
        if args.name_format is not None:
            return _format(check_name_format, args.name_format, "The name")
        return _check()
    except (_Refused, StateError) as error:
        print(error, file=sys.stderr)
        return FAILED


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-bridge", description="Run agent-bridge and join it to a gateway.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the service, from the env file and state file")
    enroll_command = commands.add_parser("enroll", help="join the gateway that printed the string")
    enroll_command.add_argument("string", help="what `sudo mcp-gateway enroll` printed, crm1@…")
    commands.add_parser("unenroll", help="leave the gateway")
    check = commands.add_parser("check", help="check the running service; or check a value's format")
    formats = check.add_mutually_exclusive_group()
    formats.add_argument("--token-format", metavar="TOKEN", help="exit 0 if TOKEN can be AGENT_BRIDGE_TOKEN, else 2")
    formats.add_argument("--name-format", metavar="NAME", help="exit 0 if NAME can be AGENT_BRIDGE_NAME, else 2")
    return parser


def _settings(for_service: bool = True) -> Settings:
    try:
        settings = Settings.from_env()
    except RuntimeError as error:
        raise _Refused(str(error)) from None
    if for_service and (missing := settings.missing_for_service()):
        raise _Refused(f"Set {', '.join(missing)} in {env_file()}.")
    return settings


def _serve(run: Callable[..., None]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-9s %(message)s")
    settings = _settings()
    path = state_file()
    app = create_app(settings, state=load_or_create(path), state_path=path)
    run(app, host=settings.host, port=settings.port, server_header=False)
    return OK


def _enroll(string: str) -> int:
    try:
        gateway_url, secret = parse_enrollment(string)
    except ValueError as error:
        raise _Refused(str(error)) from None
    settings = _settings()
    path = state_file()
    state = load_or_create(path)
    try:
        reply = asyncio.run(enroll(gateway_url, secret, state, settings))
    except GatewayUnreachable as error:
        print(f"Couldn't enroll: {error}. Once it is up, run again:", file=sys.stderr)
        print(f"  agent-bridge enroll '{string.strip()}'", file=sys.stderr)
        return UNREACHABLE
    except GatewayError as error:
        print(f"Couldn't enroll: {error}", file=sys.stderr)
        return NAME_TAKEN if error.code == "name_taken" else FAILED
    gateway = Gateway(url=gateway_url, gateway_id=reply["gateway_id"], name=reply["name"])
    try:
        save(path, state.model_copy(update={"gateway": gateway}))
    except StateError as error:
        # The gateway has it already; enrolling again replaces that record.
        print(f"The gateway enrolled this host as {reply['name']}, but {error} Fix that, then run again "
              "(with a new string from `sudo mcp-gateway enroll` once this one expires):", file=sys.stderr)
        print(f"  agent-bridge enroll '{string.strip()}'", file=sys.stderr)
        return FAILED
    print(f"Enrolled as {reply['name']} with the gateway at {gateway_url}.")
    if reply.get("api_version", API_VERSION) < API_VERSION:
        print("The gateway is older than this agent-bridge and may not call it: update the gateway.", file=sys.stderr)
    return OK


def _unenroll() -> int:
    path = state_file()
    state = load(path)
    gateway = state.gateway if state else None
    if state is None or gateway is None:
        print("Not enrolled with a gateway.")
        return OK
    by_hand = f"Remove it on the gateway host with: sudo mcp-gateway bridges remove {gateway.name}"
    try:
        token = _settings(for_service=False).token
        asyncio.run(unenroll(gateway.url, state, token))
    except _Refused as error:
        print(f"{error}\n{by_hand}", file=sys.stderr)
        return OK
    except GatewayUnreachable as error:
        print(f"Couldn't leave the gateway: {error}.\n{by_hand}", file=sys.stderr)
        return OK
    except GatewayError as error:
        # Already removed there, or it has another token for this host (401). Only the
        # gateway's own code says removed: its other 404s, or another server's, don't.
        if error.code != "not_enrolled":
            print(f"The gateway refused: {error}\n{by_hand}", file=sys.stderr)
            return OK
    save(path, state.model_copy(update={"gateway": None}))
    print(f"Left the gateway at {gateway.url}; {gateway.name} is no longer one of its hosts.")
    return OK


def _format(check: Callable[[str], str], value: str, what: str) -> int:
    try:
        check(value)
    except ValueError as error:
        print(f"{what} {error}.", file=sys.stderr)
        return INVALID
    return OK


def _check() -> int:
    """One line per check of the installed service."""
    failed = False

    def report(ok: bool, label: str, reason: str = "") -> None:
        nonlocal failed
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'}  {label}{f' ({reason})' if reason else ''}")

    try:
        settings = _settings()
    except _Refused as error:
        report(False, "settings", str(error).replace("\n", " "))
        return FAILED
    report(True, f"settings in {env_file()}")
    for path in (env_file(), state_file()):
        if (problem := _exposed(path)) is not None:
            report(False, f"only you can read {path}", problem)
    try:
        version = asyncio.run(Herdr(binary=settings.herdr_bin).version())
        report(True, f"herdr {version} runs")
    except HerdrError as error:
        report(False, f"herdr runs ({settings.herdr_bin})", str(error))
    state = load(state_file())
    try:
        info = httpx.get(f"{settings.url}/info", headers={"Authorization": f"Bearer {settings.token}"}, timeout=5)
        info.raise_for_status()
        same = state is not None and info.json().get("bridge_id") == state.bridge_id
        report(same, f"agent-bridge answers at {settings.url}", "" if same else "with another state file")
    except (httpx.HTTPError, ValueError) as error:
        report(False, f"agent-bridge answers at {settings.url}", type(error).__name__)
    if state is None or state.gateway is None:
        print("--    not enrolled with a gateway")
    else:
        try:
            name = asyncio.run(report_address(state.gateway.url, state, settings))["name"]
            report(True, f"the gateway at {state.gateway.url} has this host as {name}")
            rename(state_file(), state.gateway, name)
        except GatewayError as error:
            report(False, f"the gateway at {state.gateway.url} has this host", str(error))
    return FAILED if failed else OK


def _exposed(path: Path) -> str | None:
    """Why others may read `path`, if they may; Windows' per-user folders are left alone."""
    if os.name != "posix" or not path.exists():
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    return f"mode {mode:o}; chmod 600 it" if mode & 0o077 else None
