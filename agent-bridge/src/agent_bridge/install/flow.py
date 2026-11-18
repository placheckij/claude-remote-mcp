"""`agent-bridge setup` and `uninstall` as every platform runs them: install agent-bridge as a
service of the user who runs it, from the installed package, and enroll it with the gateway.

Safe to re-run, e.g. after an upgrade: it asks only for what's missing and keeps the token and
the enrollment. A host set up from a checkout is taken over the same way: its settings, token
and enrollment stay, and the service runs the package from then on. What a platform's service
is (a launch agent, a systemd unit) is its `Service`.
"""

import argparse
import contextlib
import ipaddress
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Protocol

from dotenv import dotenv_values

from agent_bridge.cli import FAILED, NAME_TAKEN, OK, SAVE_FAILED, UNREACHABLE
from agent_bridge.config import check_host, check_name_format, check_token_format
from agent_bridge.gateway import ENROLL_AGAIN, parse_enrollment
from agent_bridge.install.common import (
    PREFIX,
    Read,
    Refused,
    absolute,
    ask,
    ask_optional,
    check_settings,
    default_name,
    env_text,
    warn_about_settings_in_env,
    write_file,
)
from agent_bridge.state import Gateway, StateError, load

DEFAULT_PORT = 8765
# Half-second waits for a started bridge to answer.
CHECKS = 30
# The AGENT_BRIDGE_* settings setup writes; the env file's others are kept.
MANAGED = ("TOKEN", "ALLOWED_ROOTS", "HOST", "PORT", "NAME", "HERDR_BIN", "CLAUDE_CONFIG_DIR", "TRUST_ROOT",
           "ACCOUNTS")

TRUST_EXPLAINED = """
Folder trust (optional). The first time Claude opens in a folder it asks "Is this a
project you created or one you trust?", and an agent started remotely would quit there. agent-bridge can mark
folders under a trust root as trusted beforehand, but only in a separate Claude config
for the agents, never in your own. Answer - to skip this if you have already trusted your
project folders by opening claude in each one.
"""


@dataclass(frozen=True)
class Options:
    """The values passed in. None keeps the installed one, else asks for it (or finds it); ""
    for the optional ones is none."""

    roots: str | None = None
    name: str | None = None
    host: str | None = None
    port: str | None = None
    herdr_bin: str | None = None
    claude_config_dir: str | None = None
    trust_root: str | None = None
    enrollment: str | None = None
    accounts: str | None = None
    rotate_token: bool = False
    # Linux: a service for herdr's server; None keeps what is installed.
    herdr_service: bool | None = None

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Options":
        return cls(**{field.name: getattr(args, field.name) for field in fields(cls) if hasattr(args, field.name)})


class System(Protocol):
    """What setup asks of the host, whatever runs the service; tests replace it."""

    home: Path

    def is_root(self) -> bool: ...
    def in_checkout(self) -> bool: ...
    # Runs from an environment uv cleans away, as uvx's.
    def in_uv_cache(self) -> bool: ...
    def hostname(self) -> str: ...
    # The agent-bridge the service runs.
    def command(self) -> str: ...
    def lan_address(self) -> str | None: ...
    def has_address(self, address: str) -> bool: ...
    # Who listens on `port`, or None when nothing does.
    def port_holder(self, port: int) -> str | None: ...
    # Whether the service is what listens on `port`.
    def serves(self, port: int) -> bool: ...
    def find_herdr(self) -> str | None: ...
    # Runs the installed agent-bridge with no AGENT_BRIDGE_* in its environment, as the
    # service would; its output, stderr included, is returned when captured, else shown.
    def bridge(self, *args: str, capture: bool = False) -> tuple[int, str]: ...
    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class Installed:
    """The settings an install left: in the env file, or where a platform's older versions had
    them. Keys are upper case, the last one winning, as the bridge reads them."""

    source: str  # "env_file", a platform's older place (e.g. "plist"), or "" for no install
    values: dict[str, str | None]

    def get(self, key: str) -> str:
        return self.values.get(f"{PREFIX}{key}") or ""

    def answered(self, key: str) -> bool:
        """Whether an earlier run set it, even to nothing."""
        # The older places' versions asked every question, and left empty answers out.
        return self.source not in ("", "env_file") or f"{PREFIX}{key}" in self.values

    def others(self) -> dict[str, str]:
        """The settings setup doesn't write itself, by value: comments and hand-written
        quoting don't carry over."""
        managed = {f"{PREFIX}{key}" for key in MANAGED}
        return {key: value for key, value in self.values.items()
                if value is not None and key not in managed and (self.source == "env_file" or key.startswith(PREFIX))}


@dataclass
class Plan:
    """What setup decided before changing anything, and what it did with the gateway since."""

    values: dict[str, str]
    login_hint: str
    installed_token: str
    new_token: bool
    rotate: bool
    # The name the gateway has this host as, when it confirmed it (or couldn't be asked).
    enrolled_as: str
    # The gateway said it has no record of this host with the installed token.
    gone: bool
    # A new name, to rename on the gateway with the installed token.
    rename: bool
    enrollment: str = ""
    renamed: str = ""
    # Renamed on the gateway, but the state file still has the old name.
    unsaved_name: str = ""

    @property
    def name(self) -> str:
        return self.values[f"{PREFIX}NAME"]

    @property
    def renaming(self) -> bool:
        """A new name the gateway doesn't have yet."""
        return bool(self.enrolled_as) and self.name != self.enrolled_as and not self.renamed


@dataclass
class Enrolled:
    now: bool = False
    # The gateway took it, but the state file couldn't be saved.
    gateway_took: bool = False
    # A name the gateway has for another host, that no other name was given for.
    name_taken: str = ""


class Service(Protocol):
    """A platform's service: where its settings are, and how it is installed, started and
    removed."""

    # This kind of host in messages, as in "this Mac's LAN address".
    machine: str
    # What runs agent-bridge, as in "the launch agent must run an installed one".
    runner: str
    # The name's start when the hostname doesn't start with a letter.
    name_fallback: str
    # How often a bridge that can't listen yet tries again, as in "retries every 10 s".
    retries: str
    env_file: Path
    state_file: Path

    # Refuses what this platform can't install; nothing has changed yet.
    def check(self, plan: Plan, options: Options) -> None: ...
    # Right before the env file is written, e.g. to stop a service that holds files open.
    def prepare(self) -> None: ...
    # When setup stops after prepare(), e.g. to put back what it stopped.
    def abandon(self) -> None: ...
    # The settings a platform's older versions kept elsewhere than the env file, if any.
    def legacy(self) -> Installed | None: ...
    # Installs the service, once the env file is written.
    def install(self, plan: Plan) -> None: ...
    # (Re)starts the service with the env file as it is.
    def start(self) -> None: ...
    # The first lines of the summary: where it listens, when it starts, where its log is.
    def done(self, url: str) -> list[str]: ...
    # Stops and removes the service, and its files but the env and state files.
    def remove(self) -> None: ...
    # The folders the env and state files are in, removed when nothing else is left there.
    def folders(self) -> Iterable[Path]: ...


def setup(options: Options, system: System, service: Service, read: Read) -> int:
    plan: Plan | None = None
    try:
        plan = _decide(options, system, service, read)
        _rename(plan, system)
        plan.enrollment = _enrollment(options, plan, read)
        enrolled = Enrolled()
        try:
            service.prepare()
            _write_env(plan, service)
            service.install(plan)
            _start(system, service, plan.gone or plan.new_token)
            _enroll(plan, enrolled, system, service, read)
        except (Refused, KeyboardInterrupt):
            # However setup stops, a new token the gateway didn't confirm doesn't stay.
            if _put_token_back(plan, enrolled, system, service):
                print("note: the gateway didn't confirm the new token, so the installed one is back.",
                      file=sys.stderr)
            service.abandon()
            raise
        if _put_token_back(plan, enrolled, system, service):
            raise Refused("the gateway didn't confirm the new token, so the installed one is back; run again with "
                          "--rotate-token and a new enrollment string")
    except Refused as error:
        print(f"error: {error}", file=sys.stderr)
        if plan is not None and plan.renamed:
            print(f"note: the gateway has this host as {plan.renamed} now; the next run takes that name.",
                  file=sys.stderr)
        return FAILED
    if enrolled.name_taken:
        print(f"error: the gateway has another host named {enrolled.name_taken}; run again with another --name",
              file=sys.stderr)
        return FAILED
    _summary(plan, enrolled, system, service)
    return OK


def uninstall(system: System, service: Service) -> int:
    """Leaves the gateway, then removes the service, its settings, its state and its log."""
    if system.is_root():
        print("error: run as your own user, not with sudo", file=sys.stderr)
        return FAILED
    # First, while the token is still in the env file. It prints the gateway's command when
    # the gateway can't be reached.
    print("\n==> Leaving the gateway")
    try:
        state = load(service.state_file)
    except StateError:
        state = None
    name = state.gateway.name if state is not None and state.gateway is not None else ""
    by_hand = f"if this host was enrolled, remove it on the gateway host with: sudo mcp-gateway bridges remove {name or '<name>'}"
    try:
        if system.bridge("unenroll")[0] != OK:
            print(f"warning: agent-bridge unenroll failed (above); {by_hand}", file=sys.stderr)
    except Refused as error:
        print(f"warning: {error}; {by_hand}", file=sys.stderr)
    # unenroll can't remove a host the gateway added by hand: it has no record of it here.
    if not name:
        print("If the gateway has this host anyway (added with bridges add), remove it there: "
              "sudo mcp-gateway bridges remove <name> (see sudo mcp-gateway bridges list)")
    try:
        service.remove()
    except Refused as error:
        print(f"error: {error}", file=sys.stderr)
        return FAILED
    for path in (service.env_file, service.state_file):
        # With what an interrupted write left beside it, which for the env file holds the token.
        remove_with_temporary(path)
    for folder in service.folders():
        if folder.is_dir():
            try:
                folder.rmdir()
            except OSError:
                print(f"Left {folder}: it holds other files.")
    print("\nDone. agent-bridge is removed. Folder-trust entries it added to the agents' Claude config\n"
          "stay; they're ordinary trust entries.")
    return OK


def remove_with_temporary(path: Path) -> None:
    """Removes `path` and the temporary file write_file leaves beside it when interrupted."""
    for file in (path, path.with_name(f"{path.name}.tmp")):
        with contextlib.suppress(OSError):
            file.unlink(missing_ok=True)


def stable_command(invoked: str, which: Callable[[str], str | None] = shutil.which) -> str:
    """The path of the agent-bridge that was run, as one that survives upgrades: links are kept
    (uv tool's ~/.local/bin shim, Homebrew's bin), and a Homebrew version's own folder becomes
    its opt link."""
    if os.sep not in invoked:
        found = which(invoked)
        if found is None:
            raise Refused(f"can't find {invoked} on the PATH")
        invoked = found
    parts = Path(os.path.abspath(invoked)).parts
    if "Cellar" in parts:
        cellar = parts.index("Cellar")
        if len(parts) > cellar + 3:
            # <prefix>/Cellar/<formula>/<version>/... -> <prefix>/opt/<formula>/...
            return str(Path(*parts[:cellar], "opt", parts[cellar + 1], *parts[cellar + 3:]))
    return str(Path(*parts))


# --- Deciding: questions and checks, before anything changes ------------------------


def _decide(options: Options, system: System, service: Service, read: Read) -> Plan:
    if system.is_root():
        raise Refused("run as your own user, not with sudo: agent-bridge must run next to your herdr server")
    if system.in_checkout():
        raise Refused(
            f"this agent-bridge runs from a checkout of claude-remote-mcp, and the {service.runner} must run an "
            "installed one: install the package (uv tool install claude-remote-mcp-agent-bridge, or from the "
            "checkout uv tool install --editable ./agent-bridge) and run its agent-bridge setup"
        )
    if system.in_uv_cache():
        raise Refused(
            "this agent-bridge runs from uv's cache (uvx), which uv cleans away: install the package "
            "(uv tool install claude-remote-mcp-agent-bridge) and run its agent-bridge setup"
        )
    installed = _installed(service)
    enrolled = _enrolled(service)
    warn_about_settings_in_env()

    roots = _roots(options.roots if options.roots is not None else installed.get("ALLOWED_ROOTS")
                   or ask(read, f"Folders agents may start in, {os.pathsep}-separated", str(Path.home() / "Projects")))
    gone = False
    if installed.source == "env_file" and enrolled is not None:
        gone = _gateway_lost(system, enrolled)
        # `check` saves a name the gateway has given the host since (`bridges rename`).
        enrolled = _enrolled(service)
    name = _name(options.name, (enrolled.name if enrolled else "") or installed.get("NAME"), system, service, read)
    host = _host(options.host, installed.get("HOST"), system, service)
    port = _port(options.port, installed.get("PORT"), system, read)
    herdr = _herdr(options.herdr_bin, installed.get("HERDR_BIN"), system)
    config_dir, trust_root = _folder_trust(_kept(options.claude_config_dir, installed, "CLAUDE_CONFIG_DIR"),
                                           _kept(options.trust_root, installed, "TRUST_ROOT"), roots[0], read)
    login_hint = ""
    if config_dir and not (Path(config_dir) / ".claude.json").is_file():
        login_hint = ("Log in to Claude once in the agents' config, or new agents can't start:\n"
                      f"  CLAUDE_CONFIG_DIR={config_dir} claude")
        print(f"warning: {config_dir} has no Claude login yet (see the end)", file=sys.stderr)
    # Never asked for (it's JSON); passed in, or kept.
    accounts = options.accounts if options.accounts is not None else installed.get("ACCOUNTS")

    # Kept, unless rotating. Nobody copies it: enrolling hands it to the gateway.
    installed_token = installed.get("TOKEN")
    new_token = options.rotate_token or not installed_token
    token = secrets.token_hex(32) if new_token else installed_token
    try:
        check_token_format(token)
    except ValueError:
        raise Refused("the installed token won't do; run again with --rotate-token") from None

    values = {
        f"{PREFIX}TOKEN": token,
        f"{PREFIX}ALLOWED_ROOTS": os.pathsep.join(roots),
        f"{PREFIX}HOST": host,
        f"{PREFIX}PORT": str(port),
        f"{PREFIX}NAME": name,
        f"{PREFIX}HERDR_BIN": herdr,
        # Written even when empty, so the next run knows folder trust was answered.
        f"{PREFIX}CLAUDE_CONFIG_DIR": config_dir,
        f"{PREFIX}TRUST_ROOT": trust_root,
    }
    if accounts:
        values[f"{PREFIX}ACCOUNTS"] = accounts
    values.update(installed.others())
    check_settings(values)
    # Here, not where it's used: a rename on the gateway comes first.
    if options.enrollment:
        try:
            parse_enrollment(options.enrollment)
        except ValueError as error:
            raise Refused(str(error)) from None
    enrolled_as = enrolled.name if enrolled is not None and not gone else ""
    # A new name is renamed on the gateway with the installed token. Not with a new token,
    # which needs enrolling again, and that renames it too.
    rename = (options.name is not None and bool(enrolled_as) and name != enrolled_as and not new_token
              and installed.source == "env_file")
    plan = Plan(values, login_hint, installed_token, new_token, options.rotate_token, enrolled_as, gone, rename)
    service.check(plan, options)
    return plan


def _installed(service: Service) -> Installed:
    if service.env_file.exists():
        try:
            values = dotenv_values(service.env_file)
        except OSError as error:
            raise Refused(f"can't read {service.env_file} ({error.strerror})") from None
        return Installed("env_file", {key.upper(): value for key, value in values.items()})
    return service.legacy() or Installed("", {})


def _enrolled(service: Service) -> Gateway | None:
    try:
        state = load(service.state_file)
    except StateError as error:
        raise Refused(str(error)) from None
    return state.gateway if state is not None else None


def _kept(passed: str | None, installed: Installed, key: str) -> str | None:
    """Passed in, else the installed answer (even none), else None to ask."""
    if passed is not None:
        return passed
    return installed.get(key) if installed.answered(key) else None


def _gateway_lost(system: System, enrolled: Gateway) -> bool:
    """Whether the gateway said it has no record of this host, asked before the state file is
    trusted."""
    _, result = system.bridge("check", capture=True)
    line = next((line for line in result.splitlines() if "the gateway at" in line), "")
    # Only these two say so (enrollment.py's not_enrolled and unauthorized).
    if "isn't enrolled with the gateway" in line or "isn't this agent host's token" in line:
        print(f"warning: the gateway at {enrolled.url} didn't confirm it has this host as {enrolled.name}; "
              "enrolling again fixes that", file=sys.stderr)
        return True
    # Anything else (no answer, or a new address it couldn't reach before the service
    # restarts) leaves the record as it is; the next start reports the address by itself.
    if line.startswith("FAIL  the gateway at"):
        reason = line.split("has this host (", 1)[-1].removesuffix(")")
        print(f"warning: couldn't check with the gateway at {enrolled.url} right now ({reason}); keeping the "
              f"enrollment as {enrolled.name}", file=sys.stderr)
    return False


def _roots(answer: str) -> list[str]:
    roots = []
    for root in answer.split(os.pathsep):
        if root:
            root = absolute(root, "--roots")
            if not Path(root).is_dir():
                print(f"warning: {root} does not exist yet", file=sys.stderr)
            roots.append(root)
    if not roots:
        raise Refused("--roots needs at least one folder")
    return roots


def _name(passed: str | None, installed: str, system: System, service: Service, read: Read) -> str:
    """Passed in, else the gateway's or the installed one, else asked."""
    if passed is not None:
        try:
            return check_name_format(passed)
        except ValueError as error:
            raise Refused(f"--name {error}") from None
    if installed:
        try:
            return check_name_format(installed)
        except ValueError as error:
            print(f"The installed name {error}.", file=sys.stderr)
    default = default_name(system.hostname(), service.name_fallback)
    while True:
        name = ask(read, f"This host's name in agent ids, like {default}/w1:p1", default)
        try:
            return check_name_format(name)
        except ValueError as error:
            print(f"The name {error}.", file=sys.stderr)


def canonical(address: str) -> str:
    """An IP address as ifconfig and ip write it, lower case and compressed for IPv6; anything
    else as it is."""
    try:
        return str(ipaddress.ip_address(address))
    except ValueError:
        return address


def _host(passed: str | None, installed: str, system: System, service: Service) -> str:
    """Passed in, else the installed address while this host still has it, else its current one."""
    if passed is not None:
        host = passed
    else:
        installed = canonical(installed)
        if installed and system.has_address(installed):
            host = installed
        else:
            host = system.lan_address() or ""
            if not host:
                host = installed  # offline: kept, with the warning below
            elif installed:
                print(f"The LAN address is now {host} (was {installed}).", file=sys.stderr)
    if not host:
        raise Refused(f"couldn't find this {service.machine}'s LAN address; pass it, e.g. --host 192.168.1.20")
    try:
        host = canonical(check_host(host))
    except ValueError as error:
        raise Refused(f"--host {error}, not {host}") from None
    if not system.has_address(host):
        print(f"warning: no interface has {host} right now; agent-bridge retries {service.retries} until one does",
              file=sys.stderr)
    return host


def _port_number(value: str) -> int:
    if not re.fullmatch(r"[0-9]+", value) or not 1 <= int(value) <= 65535:
        raise ValueError(f"must be a number from 1 to 65535, not {value}")
    return int(value)


def _port(passed: str | None, installed: str, system: System, read: Read) -> int:
    # A port in use is fine only when this service holds it, e.g. the version being replaced.
    if passed is not None:
        try:
            port = _port_number(passed)
        except ValueError as error:
            raise Refused(f"--port {error}") from None
        if (holder := system.port_holder(port)) and not system.serves(port):
            raise Refused(f"Port {port} is in use by {holder}; pass another --port")
        return port
    try:
        port = _port_number(installed) if installed else DEFAULT_PORT
    except ValueError as error:
        raise Refused(f"the installed port {error}; pass another --port") from None
    while (holder := system.port_holder(port)) and not system.serves(port):
        print(f"Port {port} is in use by {holder}.", file=sys.stderr)
        free = port + 1
        while system.port_holder(free):
            free += 1
        while True:
            try:
                port = _port_number(ask(read, "Port", str(free)))
                break
            except ValueError as error:
                print(f"The port {error}.", file=sys.stderr)
    return port


def executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _herdr(passed: str | None, installed: str, system: System) -> str:
    """Passed in, else the installed one, else the one found."""
    if passed is not None:
        herdr = passed
    else:
        herdr = installed if installed and executable(installed) else system.find_herdr()
    if not herdr:
        raise Refused("herdr not found; install it, or pass --herdr-bin /full/path/to/herdr")
    herdr = absolute(herdr, "--herdr-bin")
    if not executable(herdr):
        raise Refused(f"{herdr} is not an executable")
    return herdr


def _folder_trust(config_dir: str | None, trust_root: str | None, first_root: str, read: Read) -> tuple[str, str]:
    """None asks for it; folder trust is asked once, and later runs keep the answer, even none."""
    if config_dir is None or trust_root is None:
        print(TRUST_EXPLAINED, file=sys.stderr)
    if config_dir is None:
        config_dir = ask_optional(read, "Separate Claude config folder for agents", str(Path.home() / ".claude-agents"))
    config_dir = absolute(config_dir, "--claude-config-dir")
    if trust_root is None:
        trust_root = ask_optional(read, "Trust agents' folders under", first_root) if config_dir else ""
    return config_dir, absolute(trust_root, "--trust-root")


def _rename(plan: Plan, system: System) -> None:
    """Renames the host on the gateway when it can take it that way; when it can't (too old,
    or no answer), an enrollment string is asked for, as enrolling again renames it too."""
    if not plan.rename:
        return
    print(f"\n==> Renaming this host to {plan.name} on the gateway")
    code, _ = system.bridge("rename", plan.name)
    if code == NAME_TAKEN:
        raise Refused(f"the gateway has another host named {plan.name}; nothing was changed, run again with "
                      "another --name")
    if code in (OK, SAVE_FAILED):
        plan.renamed = plan.name
    if code == SAVE_FAILED:
        plan.unsaved_name = plan.name


def _enrollment(options: Options, plan: Plan, read: Read) -> str:
    """Asked for until this host is enrolled, and again with a new token or a new name."""
    if options.enrollment is not None:
        enrollment = options.enrollment.strip()  # checked in _decide
    elif not plan.enrolled_as or plan.new_token or plan.renaming:
        print("\nTo enroll with the gateway, run on the gateway host: sudo mcp-gateway enroll", file=sys.stderr)
        if plan.renaming:
            print(f"The gateway has this host as {plan.enrolled_as}; enrolling again renames it {plan.name}.",
                  file=sys.stderr)
        enrollment = _ask_enrollment(read, "Enrollment string it printed, crm1@…")
    else:
        enrollment = ""
    if plan.rotate and not enrollment:
        raise Refused("--rotate-token needs an enrollment string, so the gateway learns the new token: run sudo "
                      "mcp-gateway enroll on the gateway host and pass what it prints as --enrollment")
    return enrollment


def _ask_enrollment(read: Read, question: str) -> str:
    """A valid string, or "" to enroll later."""
    while answer := ask(read, question, "", "Enter to enroll later"):
        try:
            parse_enrollment(answer)
            return answer
        except ValueError as error:
            print(error, file=sys.stderr)
    return ""


# --- Installing and enrolling ------------------------------------------------------


def _write_env(plan: Plan, service: Service) -> None:
    write_file(service.env_file, env_text(plan.values).encode(), 0o600, folder_mode=0o700)


def _start(system: System, service: Service, enrolls: bool) -> None:
    """Starts the service and prints its checks; `enrolls`: setup enrolls next, or says how at
    the end (the gateway lost this host, or the token is new), so check's own advice to enroll
    again is left out."""
    service.start()
    print("\n==> Checks")
    for _ in range(CHECKS):
        _, result = system.bridge("check", capture=True)
        if "FAIL  agent-bridge answers" not in result:
            break
        system.sleep(0.5)
    print(result.replace(f" {ENROLL_AGAIN}", "") if enrolls else result, end="")


def _enroll(plan: Plan, enrolled: Enrolled, system: System, service: Service, read: Read) -> None:
    """Records in `enrolled` how far it got."""
    while plan.enrollment:
        print("\n==> Enrolling with the gateway")
        code, _ = system.bridge("enroll", plan.enrollment)
        if code == OK:
            enrolled.now = True
            break
        if code == UNREACHABLE:  # it printed the command to run once the gateway is up
            break
        if code == SAVE_FAILED:
            # The gateway has it: another string wouldn't help with the state file.
            enrolled.gateway_took = True
            break
        if code == NAME_TAKEN:
            taken = plan.name
            # It drops an old record only at the same address, e.g. one added with bridges add.
            print(f"If {taken} is this {service.machine}'s old record (its address has changed), remove it on "
                  f"the gateway host with: sudo mcp-gateway bridges remove {taken}, then run again. Otherwise, "
                  "choose another name.", file=sys.stderr)
            name = _another_name(read)
            if not name:
                enrolled.name_taken = taken
                break
            plan.values[f"{PREFIX}NAME"] = name
            _write_env(plan, service)
            _start(system, service, plan.gone or plan.new_token)
            continue
        plan.enrollment = _ask_enrollment(read, "Enrollment string, crm1@…")


def _another_name(read: Read) -> str:
    """A valid name, or "" when none is given."""
    while name := ask(read, "Another name for this host", "", "required"):
        try:
            return check_name_format(name)
        except ValueError as error:
            print(f"The name {error}.", file=sys.stderr)
    return ""


def _put_token_back(plan: Plan, enrolled: Enrolled, system: System, service: Service) -> bool:
    """Whether the installed token went back. Unless the gateway took the new one, it still
    has the installed one, and the new one would lock it out. `enroll` can fail after the
    gateway took it (a state file it can't write, or a lost answer), so the gateway is asked;
    without that, the installed token goes back on the chance the gateway has it."""
    if not plan.rotate or enrolled.now or enrolled.gateway_took or not plan.installed_token:
        return False
    try:
        _, result = system.bridge("check", capture=True)
    except Refused:
        result = ""
    if "ok    the gateway at" in result:
        enrolled.now = True
        return False
    plan.values[f"{PREFIX}TOKEN"] = plan.installed_token
    _write_env(plan, service)
    # A service that wouldn't start before may not now either; its next start reads it.
    with contextlib.suppress(Refused):
        _start(system, service, plan.gone or plan.new_token)
    return True


def _summary(plan: Plan, enrolled: Enrolled, system: System, service: Service) -> None:
    host, port = plan.values[f"{PREFIX}HOST"], plan.values[f"{PREFIX}PORT"]
    url_host = f"[{host}]" if ":" in host else host
    print()
    for line in service.done(f"http://{url_host}:{port}"):
        print(line)
    if plan.login_hint:
        print(f"\n{plan.login_hint}")
    try:
        gateway = _enrolled(service)
    except Refused:
        gateway = None
    name = (plan.unsaved_name or gateway.name) if gateway is not None else ""
    later = f"  {system.command()} enroll '<what it printed>'"
    if enrolled.gateway_took:
        print("\nThe gateway enrolled it, but its state file couldn't be written (see above). Fix that, then "
              f"enroll again with a new string from sudo mcp-gateway enroll:\n{later}")
    elif gateway is None:
        print(f"\nIt is not enrolled with a gateway yet. Run sudo mcp-gateway enroll on the gateway host, then "
              f"here:\n{later}")
    elif not enrolled.now and plan.new_token:
        print(f"\nThe gateway has it as {name}, but with an older token, so it can't reach it. Run sudo "
              f"mcp-gateway enroll on the gateway host, then here:\n{later}")
    elif not enrolled.now and plan.gone:
        print(f"\nThe gateway at {gateway.url} didn't confirm it has it as {name} (see the warning above). Run "
              f"sudo mcp-gateway enroll on the gateway host, then here:\n{later}")
    else:
        print(f"\nIt is enrolled with the gateway at {gateway.url} as {name}: Claude reaches its agents as "
              f"{name}/…")
        if plan.renaming and not enrolled.now:
            print(f"The gateway still has it as {name}, not {plan.name}: run again with --name {plan.name} and an "
                  "enrollment string to rename it.")


# --- What macOS and Linux run the same way --------------------------------------------


class PosixSystem:
    """The parts of a real Mac or Linux host that don't depend on its service manager."""

    # Where herdr is looked for when it isn't on the PATH.
    herdr_places: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.home = Path.home()

    def is_root(self) -> bool:
        return os.geteuid() == 0

    def in_checkout(self) -> bool:
        # A checkout's agent-bridge runs from <checkout>/.venv.
        return (Path(sys.prefix).parent / "agent-bridge" / "pyproject.toml").is_file()

    def in_uv_cache(self) -> bool:
        # uvx's environments are <uv cache>/archive-v0/<id>.
        return Path(sys.prefix).parent.name.startswith("archive-v")

    def hostname(self) -> str:
        return socket.gethostname()

    def command(self) -> str:
        return stable_command(sys.argv[0])

    def find_herdr(self) -> str | None:
        """The herdr on PATH, else one in the usual places."""
        for path in (shutil.which("herdr"), self.home / ".local/bin/herdr", *self.herdr_places):
            if path and executable(str(path)):
                return str(path)
        return None

    def bridge(self, *args: str, capture: bool = False) -> tuple[int, str]:
        environment = {name: value for name, value in os.environ.items() if not name.startswith(PREFIX)}
        # What was printed so far goes first: piped, Python holds it while the child writes.
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            # Captured, its errors (e.g. an unreadable state file) stay in what is shown.
            captured = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT} if capture else {}
            result = subprocess.run([self.command(), *args], env=environment, cwd="/", text=True, **captured)
        except OSError as error:
            raise Refused(f"can't run {self.command()} ({error.strerror})") from None
        return result.returncode, result.stdout or ""

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def output(*argv: str) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True).stdout
    except OSError:
        return ""
