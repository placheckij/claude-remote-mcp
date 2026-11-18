"""`agent-bridge setup` and `uninstall` on Linux: agent-bridge as a systemd user unit of the
user who runs it, and herdr's server as another, so agents can start after a reboot. With
lingering on, both start at boot without anyone logging in. A re-run never restarts herdr's
server, so running agents stay. The rest of setup is flow's.
"""

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Protocol

from dotenv import dotenv_values

from agent_bridge.install import flow
from agent_bridge.install.common import PREFIX, Read, Refused, check_settings, write_file
from agent_bridge.install.flow import Installed, Options, Plan, PosixSystem, output, stable_command

__all__ = ["HERDR_UNIT", "UNIT", "LinuxService", "LinuxSystem", "Options", "setup", "stable_command", "system", "uninstall",
           "unit"]

UNIT = "agent-bridge.service"
HERDR_UNIT = "agent-bridge-herdr.service"


class System(flow.System, Protocol):
    # Whether `systemctl --user` reaches this user's service manager.
    def has_user_manager(self) -> bool: ...
    def systemctl(self, *args: str) -> int: ...
    # The unit's main PID, or None when it doesn't run.
    def main_pid(self, unit: str) -> str | None: ...
    def lingering(self) -> bool: ...
    def enable_linger(self) -> bool: ...
    def herdr_running(self, herdr: str) -> bool: ...
    def user(self) -> str: ...


def setup(options: Options, system: System, read: Read) -> int:
    if _no_user_manager(system):
        return flow.FAILED
    return flow.setup(options, system, LinuxService(system), read)


def uninstall(system: System) -> int:
    if _no_user_manager(system):
        return flow.FAILED
    return flow.uninstall(system, LinuxService(system))


def _no_user_manager(system: System) -> bool:
    """Said before any question. Root is left to flow, which says to run as your own user."""
    if system.is_root() or system.has_user_manager():
        return False
    print(f"error: {NO_USER_MANAGER}", file=sys.stderr)
    return True


NO_USER_MANAGER = ("can't reach your systemd user manager; run this from your own login (SSH or a console), "
                   "not through su or sudo -u")


def unit(name: str, placeholder: str, value: str) -> str:
    """The packaged unit `name` with `value` in place of `@placeholder@`, where the unit
    double-quotes it: systemd reads % as a specifier and $ as a variable, and can't take " or \\
    there."""
    if re.search(r'["\\\n]', value):
        raise Refused(f"systemd can't run a program whose path has \", \\ or a line break in it: {value}")
    template = resources.files("agent_bridge.install").joinpath("systemd", name).read_text(encoding="utf-8")
    return template.replace(f"@{placeholder}@", value.replace("%", "%%").replace("$", "$$"))


@dataclass
class LinuxService:
    system: System
    machine: str = "host"
    runner: str = "systemd unit"
    name_fallback: str = "linux"
    retries: str = "every 10 s"
    herdr_service: bool = True
    linger_hint: str = field(default="", init=False)

    @property
    def env_file(self) -> Path:
        # Where `agent-bridge serve` reads its settings, as systemd starts it with none.
        return self.system.home / ".config" / "agent-bridge" / "env"

    @property
    def state_file(self) -> Path:
        return self.system.home / ".local" / "state" / "agent-bridge" / "state.json"

    @property
    def unit_dir(self) -> Path:
        return self.system.home / ".config" / "systemd" / "user"

    def check(self, plan: Plan, options: Options) -> None:
        # Kept as installed unless passed: a host set up without it (HERDR_SERVICE=0 with
        # setup.sh) stays so, and a new one gets it.
        if options.herdr_service is not None:
            self.herdr_service = options.herdr_service
        elif (self.unit_dir / UNIT).exists():
            self.herdr_service = (self.unit_dir / HERDR_UNIT).exists()
        self._check_legacy_env(plan)
        # Refused here, before anything changes, rather than when the units are written.
        unit(UNIT, "AGENT_BRIDGE", self.system.command())
        if self.herdr_service:
            unit(HERDR_UNIT, "HERDR_BIN", plan.values[f"{flow.PREFIX}HERDR_BIN"])

    def _check_legacy_env(self, plan: Plan) -> None:
        """systemd starts a user unit in the home folder, where serve still reads a .env (the
        env file wins): the settings are checked as it will read them both."""
        legacy = self.system.home / ".env"
        try:
            values = dotenv_values(legacy) if legacy.is_file() else {}
        except OSError as error:
            raise Refused(f"can't read {legacy} ({error.strerror})") from None
        older = {key.upper(): value for key, value in values.items()
                 if value is not None and key.upper().startswith(PREFIX)}
        if older:
            print(f"warning: agent-bridge also reads the settings in {legacy}; move them to {self.env_file}",
                  file=sys.stderr)
            try:
                check_settings({**older, **plan.values})
            except Refused as error:
                raise Refused(f"with the settings in {legacy}, {error}") from None

    def prepare(self) -> None:
        pass

    def abandon(self) -> None:
        pass

    def legacy(self) -> Installed | None:
        # setup.sh wrote the same env file.
        return None

    def install(self, plan: Plan) -> None:
        herdr = plan.values[f"{flow.PREFIX}HERDR_BIN"]
        # herdr's first: a re-run reads an agent-bridge unit without it as no herdr service.
        if self.herdr_service:
            self._write(HERDR_UNIT, unit(HERDR_UNIT, "HERDR_BIN", herdr))
        self._write(UNIT, unit(UNIT, "AGENT_BRIDGE", self.system.command()))
        self.system.systemctl("daemon-reload")
        self._herdr(herdr)
        # Lingering starts the user's services at boot and keeps them after logout.
        if not self.system.lingering():
            print("\n==> Turning on lingering, so the services run without a login")
            if not self.system.enable_linger():
                self.linger_hint = ("Lingering is off, so agent-bridge stops at your last logout and doesn't start "
                                    f"at boot. Turn it on with:\n  sudo loginctl enable-linger {self.system.user()}")

    def _write(self, name: str, text: str) -> None:
        print(f"\n==> Writing {self.unit_dir / name}")
        write_file(self.unit_dir / name, text.encode(), 0o644)

    def _herdr(self, herdr: str) -> None:
        if not self.herdr_service:
            if (self.unit_dir / HERDR_UNIT).exists():
                print(f"\n==> Removing {HERDR_UNIT} (its server keeps running until it stops)")
                self._remove_herdr_unit()
                self.system.systemctl("daemon-reload")
            return
        if self.system.systemctl("enable", "--quiet", HERDR_UNIT) != 0:
            raise Refused(f"systemctl --user enable {HERDR_UNIT} failed")
        # Never restarted: that would stop every agent. A changed unit applies at its next start.
        if self.system.main_pid(HERDR_UNIT):
            print(f"herdr's server runs as {HERDR_UNIT}.")
        elif self.system.herdr_running(herdr):
            print(f"herdr's server already runs outside systemd; {HERDR_UNIT} starts it from the next boot.")
        else:
            print(f"\n==> Starting herdr's server ({HERDR_UNIT})")
            if self.system.systemctl("start", HERDR_UNIT) != 0:
                print(f"warning: {HERDR_UNIT} didn't start; see: journalctl --user -u {HERDR_UNIT}",
                      file=sys.stderr)

    def _remove_herdr_unit(self) -> None:
        # Not stopped: that would stop every agent in herdr.
        self.system.systemctl("disable", "--quiet", HERDR_UNIT)
        self._unlink(HERDR_UNIT)

    def _unlink(self, name: str) -> None:
        path = self.unit_dir / name
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise Refused(f"can't remove {path} ({error.strerror})") from None
        flow.remove_with_temporary(path)

    def start(self) -> None:
        print(f"\n==> Starting {UNIT}")
        if self.system.systemctl("enable", "--quiet", UNIT) != 0 or self.system.systemctl("restart", UNIT) != 0:
            raise Refused(f"systemctl --user couldn't start {UNIT}; see: journalctl --user -u {UNIT}")

    def done(self, url: str) -> list[str]:
        lines = [f"Done. agent-bridge listens on {url} and starts at boot.",
                 f"If a check failed: journalctl --user -u {UNIT} (herdr failing means its server isn't running)."]
        if self.linger_hint:
            lines += ["", self.linger_hint]
        return lines

    def remove(self) -> None:
        print(f"\n==> Removing {UNIT}")
        self.system.systemctl("disable", "--now", "--quiet", UNIT)
        self._unlink(UNIT)
        herdr_note = ""
        if (self.unit_dir / HERDR_UNIT).exists():
            print(f"\n==> Removing {HERDR_UNIT}")
            running = self.system.main_pid(HERDR_UNIT)
            self._remove_herdr_unit()
            if running:
                herdr_note = ("herdr's server keeps running with its agents until you stop it (herdr server stop) "
                              "or reboot.")
        self.system.systemctl("daemon-reload")
        self.system.systemctl("reset-failed", UNIT)
        if herdr_note:
            print(herdr_note)
        print(f"Its log is in the journal, which systemd ages out: journalctl --user -u {UNIT}\n"
              f"Lingering stays on, for any other user services. To turn it off:\n"
              f"  loginctl disable-linger {self.system.user()}")

    def folders(self) -> Iterable[Path]:
        return (self.env_file.parent, self.state_file.parent)


class LinuxSystem(PosixSystem):
    herdr_places = ("/usr/local/bin/herdr", "/usr/bin/herdr")

    def lan_address(self) -> str | None:
        # The source address of the default route; nothing is sent.
        found = re.search(r"\bsrc (\S+)", output("ip", "-4", "route", "get", "1.1.1.1"))
        return found.group(1) if found else None

    def has_address(self, address: str) -> bool:
        if address == "127.0.0.1":
            return True
        for line in output("ip", "-o", "addr", "show").splitlines():
            words = line.split()
            for kind, value in zip(words, words[1:], strict=False):
                if kind in ("inet", "inet6") and flow.canonical(value.split("/")[0]) == address:
                    return True
        return False

    def _listeners(self, port: int) -> list[str]:
        # Without ss, every port would look free.
        if shutil.which("ss") is None:
            raise Refused("ss not found, which setup needs to see who holds a port; install iproute2")
        return output("ss", "-Hltnp", f"sport = :{port}").splitlines()

    def port_holder(self, port: int) -> str | None:
        lines = self._listeners(port)
        if not lines:
            return None
        # ss shows the PID of your own processes only.
        pid = re.search(r"pid=(\d+)", "\n".join(lines))
        if pid is None:
            return "another user's program"
        command = output("ps", "-o", "args=", "-p", pid.group(1)).strip()
        return f"PID {pid.group(1)}: {command}" if command else f"PID {pid.group(1)}"

    def serves(self, port: int) -> bool:
        # The unit's main process (the version being replaced has the same unit). Not by
        # command line: a bridge started by hand can run the same command, and keeps the port.
        main = self.main_pid(UNIT)
        return main is not None and main in re.findall(r"pid=(\d+)", "\n".join(self._listeners(port)))

    def has_user_manager(self) -> bool:
        return self.systemctl("show-environment") == 0

    def systemctl(self, *args: str) -> int:
        try:
            # Its errors are said by setup, e.g. reset-failed's for a unit just removed.
            return subprocess.run(["systemctl", "--user", *args], capture_output=True).returncode
        except OSError:
            return 1

    def main_pid(self, unit: str) -> str | None:
        pid = output("systemctl", "--user", "show", "-p", "MainPID", "--value", unit).strip()
        return pid if pid not in ("", "0") else None

    def lingering(self) -> bool:
        return output("loginctl", "show-user", self.user(), "-p", "Linger", "--value").strip() == "yes"

    def enable_linger(self) -> bool:
        try:
            return subprocess.run(["loginctl", "enable-linger", self.user()], capture_output=True).returncode == 0
        except OSError:
            return False

    def herdr_running(self, herdr: str) -> bool:
        return any(line.strip() == "status: running" for line in output(herdr, "status", "server").splitlines())

    def user(self) -> str:
        import pwd  # not on Windows, where the tests import this module too

        return pwd.getpwuid(os.getuid()).pw_name


def system() -> LinuxSystem:
    return LinuxSystem()
