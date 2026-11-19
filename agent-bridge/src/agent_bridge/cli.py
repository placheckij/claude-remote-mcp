"""`agent-bridge serve | enroll | rename | unenroll | check | setup | uninstall`: what the platform setups run.

Exit codes are part of the deploy contract the setups are built against: `enroll` and
`rename` exit 3 when the name is taken (ask for another) and 4 when the gateway can't be
reached; both exit 5 when the gateway took it but the state file couldn't be saved (the
gateway has the new token and name, so keep them); `rename` exits 6 when the
gateway is too old to rename hosts (rename it there); `rename`, `check --token-format` and
`--name-format` exit 2 for a value that won't do.
"""

import argparse
import asyncio
import contextlib
import locale
import logging
import logging.handlers
import os
import stat
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

import httpx
import uvicorn

from agent_bridge.app import create_app
from agent_bridge.config import Settings, check_name_format, check_token_format
from agent_bridge.gateway import (
    ENROLL_AGAIN,
    GatewayError,
    GatewayUnreachable,
    enroll,
    parse_enrollment,
    rename_host,
    report_address,
    unenroll,
)
from agent_bridge.herdr import Herdr, HerdrError
from agent_bridge.host import API_VERSION, bridge_version
from agent_bridge.state import Gateway, State, StateError, env_file, load, load_or_create, rename, save, state_file

OK, FAILED, INVALID, NAME_TAKEN, UNREACHABLE, SAVE_FAILED, OUTDATED = 0, 1, 2, 3, 4, 5, 6
# A --log-file larger than this is set aside as <name>.1.
LOG_LIMIT = 1024 * 1024
# The gateway's codes for a host it has no record of with this token (enrollment.py).
NOT_KNOWN = ("not_enrolled", "unauthorized")

log = logging.getLogger(__name__)


class _Refused(Exception):
    """What to print before exiting with 1."""


def main(argv: list[str] | None = None, *, run: Callable[..., None] = uvicorn.run) -> int:
    """`run` serves the app; tests replace it."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "serve":
            return _serve(run, args.log_file)
        if args.command == "enroll":
            return _enroll(args.string)
        if args.command == "rename":
            return _rename(args.name)
        if args.command == "unenroll":
            return _unenroll()
        if args.command == "setup":
            return _setup(args)
        if args.command == "uninstall":
            return _uninstall()
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
    parser.add_argument("--version", action="version", version=f"%(prog)s {bridge_version()}")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the service, from the env file and state file")
    serve.add_argument("--log-file", type=Path, metavar="PATH", help="append the log to PATH instead of stderr")
    enroll_command = commands.add_parser("enroll", help="join the gateway that printed the string")
    enroll_command.add_argument("string", help="what `sudo mcp-gateway enroll` printed, crm1@…")
    rename_command = commands.add_parser("rename", help="rename this host on the gateway")
    rename_command.add_argument("name", help="the new name, like linux-box")
    commands.add_parser("unenroll", help="leave the gateway")
    check = commands.add_parser("check", help="check the running service; or check a value's format")
    formats = check.add_mutually_exclusive_group()
    formats.add_argument("--token-format", metavar="TOKEN", help="exit 0 if TOKEN can be AGENT_BRIDGE_TOKEN, else 2")
    formats.add_argument("--name-format", metavar="NAME", help="exit 0 if NAME can be AGENT_BRIDGE_NAME, else 2")
    setup = commands.add_parser(
        "setup", help="install or update the service on this host and enroll it; keeps what is installed, "
                      "and asks for what is missing",
    )
    setup.add_argument("--roots", metavar="DIRS", help=f"the {os.pathsep}-separated folders agents may start in")
    setup.add_argument("--name", help="this host's name in agent ids (default: the hostname); a new one on an "
                                      "enrolled host renames it on the gateway")
    setup.add_argument("--host", metavar="ADDRESS", help="the LAN address to listen on (default: found)")
    setup.add_argument("--port", help="the port to listen on (default: 8765)")
    setup.add_argument("--herdr-bin", metavar="PATH", help="herdr (default: found)")
    setup.add_argument("--claude-config-dir", metavar="DIR", help="a separate Claude config for agents; '' for none")
    setup.add_argument("--trust-root", metavar="DIR", help="trust agents' folders under DIR in that config; '' for none")
    setup.add_argument("--enrollment", metavar="STRING", help="what `sudo mcp-gateway enroll` printed; '' to enroll later")
    setup.add_argument("--accounts", metavar="JSON", help="AGENT_BRIDGE_ACCOUNTS, the named Claude logins; '' removes them")
    setup.add_argument("--rotate-token", action="store_true",
                       help="a new token; needs --enrollment, so the gateway learns it")
    setup.add_argument("--herdr-service", action=argparse.BooleanOptionalAction, default=None,
                       help="Linux: run herdr's server as a user service too (default: as installed, else yes)")
    commands.add_parser("uninstall", help="leave the gateway, then remove the service, its settings, state and log")
    return parser


def _settings(for_service: bool = True) -> Settings:
    try:
        settings = Settings.from_env()
    except RuntimeError as error:
        raise _Refused(str(error)) from None
    if for_service and (missing := settings.missing_for_service()):
        raise _Refused(f"Set {', '.join(missing)} in {env_file()}.")
    return settings


def _serve(run: Callable[..., None], log_file: Path | None) -> int:
    if log_file is None:
        logging.basicConfig(level=logging.INFO, format="%(levelname)-9s %(message)s")
        handler = None
    else:
        handler = _open_log(log_file)
    try:
        settings = _settings()
        path = state_file()
        app = create_app(settings, state=load_or_create(path), state_path=path)
        # uvicorn's loggers then reach the log file too, through the root logger.
        options = {"log_config": None} if handler else {}
        run(app, host=settings.host, port=settings.port, server_header=False, **options)
    except (_Refused, StateError) as error:
        # Printed too, where no one may see it (the Windows task has no console).
        if handler:
            log.error("Not starting: %s", error)
        raise
    except Exception as error:
        # Its type alone: the message or traceback may hold a setting.
        log.error("agent-bridge stopped: %s", type(error).__name__)
        raise
    finally:
        if handler:
            logging.getLogger().removeHandler(handler)
            handler.close()
    return OK


class _NoTraceback(logging.Formatter):
    """Writes a logged exception as its type alone: its message or traceback may hold a
    setting. Works on a copy, since another handler may have put the traceback in the
    record already."""

    def format(self, record: logging.LogRecord) -> str:
        copy = logging.makeLogRecord(record.__dict__)
        kind = record.exc_info[0] if record.exc_info else None
        copy.exc_text = f"{kind.__name__} (traceback left out)" if kind else None
        copy.exc_info = copy.stack_info = None
        if kind and isinstance(copy.msg, str):
            copy.msg = copy.msg.rstrip()
        return super().format(copy)


class _RotatingLog(logging.handlers.RotatingFileHandler):
    """Moved to `<name>.1` once past `maxBytes`. On Windows that fails while another process
    has it open, e.g. `Get-Content -Wait`: it is then appended to and tried again at the next
    line, where RotatingFileHandler would drop each line until it can."""

    def doRollover(self) -> None:
        # Not RotatingFileHandler's: it deletes the old `.1` before it tries the move.
        if self.stream:
            self.stream.close()
            self.stream = None
        with contextlib.suppress(OSError):
            os.replace(self.baseFilename, f"{self.baseFilename}.1")
        self.stream = self._open()


def _open_log(path: Path) -> logging.Handler:
    """Sends logging, and only logging, to `path`, after a line for this start. What is
    printed stays out of it, as do tracebacks: they may hold settings. The log is moved to
    `<name>.1` once it passes 1 MB, also while agent-bridge runs."""
    with contextlib.suppress(OSError):  # not there yet, or held open by another bridge
        if path.stat().st_size > LOG_LIMIT:
            os.replace(path, path.with_name(f"{path.name}.1"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = _RotatingLog(path, maxBytes=LOG_LIMIT, backupCount=1, encoding="utf-8")
    except OSError as error:
        raise _Refused(f"Can't write {path} ({error.strerror}).") from None
    handler.stream.write(f"==== {datetime.now():%Y-%m-%d %H:%M:%S} starting agent-bridge\n")
    handler.setFormatter(_NoTraceback("%(asctime)s %(levelname)-9s %(message)s", "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return handler


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
        return SAVE_FAILED
    print(f"Enrolled as {reply['name']} with the gateway at {gateway_url}.")
    if reply.get("api_version", API_VERSION) < API_VERSION:
        print("The gateway is older than this agent-bridge and may not call it: update the gateway.", file=sys.stderr)
    return OK


def _rename(name: str) -> int:
    """Renames this host on the gateway and saves the name to the state file; the env
    file's AGENT_BRIDGE_NAME is the setups' to write."""
    if _format(check_name_format, name, "The name") != OK:
        return INVALID
    path = state_file()
    state = load(path)
    gateway = state.gateway if state else None
    if state is None or gateway is None:
        print("Not enrolled with a gateway, so there is nothing to rename: enroll with the new name.", file=sys.stderr)
        return FAILED
    settings = _settings()
    by_hand = f"Rename it on the gateway host with: sudo mcp-gateway bridges rename {gateway.name} {name}"
    try:
        reply = asyncio.run(rename_host(gateway.url, state, settings.token, name))
    except GatewayUnreachable as error:
        print(f"Couldn't rename: {error}.\n{by_hand}", file=sys.stderr)
        return UNREACHABLE
    except GatewayError as error:
        if error.code == "name_taken":
            print(f"Couldn't rename: {error}", file=sys.stderr)
            return NAME_TAKEN
        refusal = error.code
        if error.status == 404 and refusal is None:
            # No such route, or the gateway refusing a caller off the LAN: the address report,
            # which every gateway has, tells them apart.
            now, refusal = _address_report(gateway.url, state, settings)
            # A refused address (e.g. one it can't reach while agent-bridge is stopped) still
            # took the token, which it checks first.
            if now is not None or (refusal is not None and refusal not in NOT_KNOWN):
                by_hand = ("Rename it on the gateway host with: "
                           f"sudo mcp-gateway bridges rename {now or gateway.name} {name}")
                print(f"The gateway at {gateway.url} is too old to rename hosts: update the gateway.\n{by_hand}",
                      file=sys.stderr)
                return OUTDATED
        if refusal in NOT_KNOWN:
            # It has no host by the old name to rename by hand.
            print(f"The gateway refused: {error} Enroll again under the new name: run `sudo mcp-gateway enroll` "
                  "on the gateway host for a string.", file=sys.stderr)
            return FAILED
        print(f"The gateway refused: {error}\n{by_hand}", file=sys.stderr)
        return FAILED
    try:
        rename(path, gateway, reply["name"])
    except StateError as error:
        # The gateway has the new name already, so its command for the old one would fail.
        print(f"The gateway at {gateway.url} has this host as {reply['name']} now, but {error} Fix that, then run "
              "`agent-bridge check`, which saves the gateway's name.", file=sys.stderr)
        return SAVE_FAILED
    print(f"The gateway at {gateway.url} has this host as {reply['name']}.")
    return OK


def _address_report(gateway_url: str, state: State, settings: Settings) -> tuple[str | None, str | None]:
    """The gateway's name for this host from its answer to the address report, or the code
    it refused it with (None for no answer, or not the gateway's own)."""
    try:
        return asyncio.run(report_address(gateway_url, state, settings))["name"], None
    except GatewayError as error:
        return None, error.code


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


def _setup(args: argparse.Namespace) -> int:
    platform = _platform("setup")
    if platform is None:
        return FAILED
    options = platform.Options.from_args(args)
    return platform.setup(options, platform.system(), lambda: read_line(sys.stdin.buffer))


def read_line(stream: BinaryIO) -> str | None:
    """The next line of setup's answers, None at the end. UTF-8 without the byte order mark
    Windows PowerShell 5.1 puts first when it pipes, which Python would decode in the ANSI
    code page; anything else in the locale's encoding."""
    line = stream.readline()
    if not line:
        return None
    try:
        return line.decode("utf-8-sig")
    except UnicodeDecodeError:
        return line.decode(locale.getpreferredencoding(False), "replace")


def _uninstall() -> int:
    platform = _platform("uninstall")
    if platform is None:
        return FAILED
    return platform.uninstall(platform.system())


def _platform(command: str):  # noqa: ANN202 - the platform's install module
    if sys.platform == "darwin":
        from agent_bridge.install import macos

        return macos
    if sys.platform.startswith("linux"):
        from agent_bridge.install import linux

        return linux
    if sys.platform == "win32":
        from agent_bridge.install import windows

        return windows
    print(f"agent-bridge {command} is for macOS, Linux and Windows.", file=sys.stderr)
    return None


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
    herdr = Herdr(binary=settings.herdr_bin)
    try:
        version = asyncio.run(herdr.version())
        report(True, f"herdr {version} runs")
    except HerdrError as error:
        report(False, f"herdr runs ({settings.herdr_bin})", str(error))
    else:
        try:
            report(*_herdr_server(asyncio.run(herdr.server_status()), version))
        except HerdrError as error:
            report(False, "herdr's server runs", str(error))
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
            hint = f" {ENROLL_AGAIN}" if error.code in NOT_KNOWN else ""
            report(False, f"the gateway at {state.gateway.url} has this host", f"{error}{hint}")
    return FAILED if failed else OK


def _herdr_server(server: dict, version: str) -> tuple[bool, str, str]:
    """check's line for herdr's server: running, and the version of the herdr it is run with.
    A server started before `herdr update` keeps the old version until it restarts."""
    if not server.get("running"):
        return False, "herdr's server runs", "start herdr"
    if server.get("restart_needed"):
        return (False, f"herdr's server is herdr {version}",
                f"it's {server.get('version')}; restart it when its agents can stop")
    return True, f"herdr's server {server.get('version')} runs", ""


def _exposed(path: Path) -> str | None:
    """Why others may read `path`, if they may; Windows' per-user folders are left alone."""
    if os.name != "posix" or not path.exists():
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    return f"mode {mode:o}; chmod 600 it" if mode & 0o077 else None
