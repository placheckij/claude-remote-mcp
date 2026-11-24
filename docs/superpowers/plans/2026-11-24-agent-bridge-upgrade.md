# agent-bridge upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `agent-bridge upgrade` upgrades the installed package to the latest release and runs its setup again, safely on Windows by running from uvx's copy (`uvx --from claude-remote-mcp-agent-bridge@latest agent-bridge upgrade`).

**Architecture:** A new module `agent_bridge/install/upgrade.py` holds the flow against a small `System` protocol (tests use a fake). It reads uv's `uv-receipt.toml` to tell a PyPI install from a checkout/wheel install, runs `uv tool upgrade` (falling back to `--reinstall`) or `brew upgrade`, then runs the *installed* `agent-bridge setup` as a child. On Windows it first stops the scheduled task through a new `windows.stop_for_upgrade`, which reuses `WindowsService._stop`/`abandon`.

**Tech Stack:** Python 3.11+ (`tomllib`), pytest, uv tool, Windows Task Scheduler via the existing `windows.py`.

Spec: `docs/superpowers/specs/2026-11-23-agent-bridge-upgrade-design.md`.

## Global Constraints

- Package name: `claude-remote-mcp-agent-bridge`; command: `uvx --from claude-remote-mcp-agent-bridge@latest agent-bridge upgrade`. **`@latest` is required:** without it uvx reuses the installed tool's environment (verified with uv 0.13.9), which is the one Windows locks.
- Exit codes: `flow.OK` (0) on success or already up to date, `flow.FAILED` (1) on any refusal or failure; after setup runs, its exit code.
- Error lines go to stderr as `error: …`, warnings as `warning: …` (house style in `install/`).
- Tests never touch real uv, brew, Task Scheduler or processes; they run on any OS (`cd agent-bridge && uv run pytest`).
- No Claude attribution in commits. No real host names in code, docs or commits.
- Commit messages: `agent-bridge: …` prefix, plain sentence (see `git log`).

---

## File Structure

- Create `agent-bridge/src/agent_bridge/install/upgrade.py`: `Install`, `parse_receipt`, `find_install`, `run`, `System` protocol, `RealSystem`, `system()`.
- Modify `agent-bridge/src/agent_bridge/install/windows.py`: add `stop_for_upgrade` (and `_runs_from`).
- Modify `agent-bridge/src/agent_bridge/cli.py`: the `upgrade` subcommand.
- Create `agent-bridge/tests/test_upgrade.py`.
- Modify `agent-bridge/tests/test_install_windows.py`: tests for `stop_for_upgrade`.
- Modify `agent-bridge/README.md`: upgrade sections.

---

### Task 1: Reading the install

**Files:**
- Create: `agent-bridge/src/agent_bridge/install/upgrade.py`
- Test: `agent-bridge/tests/test_upgrade.py`

**Interfaces:**
- Produces:
  - `PACKAGE = "claude-remote-mcp-agent-bridge"`, `UVX: str`
  - `@dataclass(frozen=True) class Install: kind: str` (`"uv"`, `"source"` or `"Homebrew"`), `command: str`, `upgrade: tuple[str, ...]`, `environment: Path | None = None`, `programs: tuple[str, ...] = ()`, `source: str = ""`, `back: str = ""`
  - `parse_receipt(text: str, environment: Path, uv: str) -> Install` (raises `Refused`)
  - `find_install(system: System) -> Install` (raises `Refused`)
  - `class System(Protocol)` with `platform: str`, `prefix: Path`, `in_uv_cache()`, `uv()`, `tool_dir()`, `mac_installs()`, `homebrew_command()`, `version(command)`, `run(*argv)`, `run_installed(command, *args)`, `stop_bridge(install)`

- [ ] **Step 1: Write the failing tests**

Create `agent-bridge/tests/test_upgrade.py`:

```python
"""`agent-bridge upgrade`, against a fake system: reading the install from uv's receipt,
upgrading it or moving it to the published package, and running its setup again. Never runs
uv, brew or Task Scheduler, so it runs on any platform."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_bridge.install import upgrade
from agent_bridge.install.common import Refused
from agent_bridge.install.upgrade import PACKAGE

UV = "/usr/bin/uv"
PLAIN = f'{{ name = "{PACKAGE}" }}'


@dataclass
class FakeHost:
    """What upgrade asks of the host. `installed` is the version `agent-bridge --version` gives
    (None: gone or broken); an upgrade or reinstall that works installs `published`."""

    tools: Path | None
    platform: str = "linux"
    prefix: Path = Path("/home/me/.cache/uv/archive-v0/abc")
    uv_cache: bool = True
    uv_path: str | None = UV
    installed: str | None = "0.3.0"
    published: str = "0.3.1"
    upgrade_code: int = 0
    reinstall_code: int = 0
    # A failed reinstall removed the package, as an interrupted one does.
    reinstall_removes: bool = False
    installs: list[str] = field(default_factory=list)
    homebrew: str | None = None
    stop_refusal: str | None = None
    setup_code: int = 0
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def in_uv_cache(self) -> bool:
        return self.uv_cache

    def uv(self) -> str | None:
        return self.uv_path

    def tool_dir(self) -> Path | None:
        return self.tools

    def mac_installs(self) -> list[str]:
        return self.installs

    def homebrew_command(self) -> str | None:
        return self.homebrew

    def version(self, command: str) -> str | None:
        return self.installed

    def run(self, *argv: str) -> int:
        self.calls.append(("run", *argv))
        if argv[1:] in (("tool", "upgrade", PACKAGE), ("upgrade", "agent-bridge")):
            code = self.upgrade_code
        else:
            code = self.reinstall_code
            if code and self.reinstall_removes:
                self.installed = None
        if code == 0:
            self.installed = self.published
        return code

    def run_installed(self, command: str, *args: str) -> int:
        self.calls.append(("installed", command, *args, str(self.installed)))
        return self.setup_code

    def stop_bridge(self, install: upgrade.Install) -> None:
        self.calls.append(("stop", str(install.environment), *install.programs))
        if self.stop_refusal:
            raise Refused(self.stop_refusal)

    def ran(self, kind: str) -> list[tuple[str, ...]]:
        return [call[1:] for call in self.calls if call[0] == kind]


def receipt(tools: Path, requirement: str = PLAIN) -> Path:
    """A uv tool install of agent-bridge, as uv-receipt.toml has it. Returns its environment."""
    environment = tools / PACKAGE
    environment.mkdir(parents=True)
    bin = tools.parent / "bin"
    (environment / "uv-receipt.toml").write_text(
        "[tool]\n"
        f"requirements = [{requirement}]\n"
        "entrypoints = [\n"
        f'    {{ name = "agent-bridge", install-path = "{(bin / "agent-bridge").as_posix()}", from = "{PACKAGE}" }},\n'
        f'    {{ name = "agent-bridgew", install-path = "{(bin / "agent-bridgew").as_posix()}", from = "{PACKAGE}" }},\n'
        "]\n", encoding="utf-8")
    return environment


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    # uvx@latest runs the latest release.
    monkeypatch.setattr(upgrade, "BRIDGE_VERSION", "0.3.1")
    receipt(tmp_path / "tools")
    return FakeHost(tools=tmp_path / "tools")


# --- Reading the install --------------------------------------------------------------


def test_a_plain_requirement_is_upgraded_with_uv_tool_upgrade(host: FakeHost, tmp_path: Path) -> None:
    install = upgrade.find_install(host)

    assert install.kind == "uv"
    assert install.upgrade == (UV, "tool", "upgrade", PACKAGE)
    assert install.command == (tmp_path / "bin" / "agent-bridge").as_posix()
    assert install.environment == tmp_path / "tools" / PACKAGE
    assert install.programs == ((tmp_path / "bin" / "agent-bridge").as_posix(),
                                (tmp_path / "bin" / "agent-bridgew").as_posix())


def test_a_pinned_requirement_is_still_from_the_package_index() -> None:
    text = (f'[tool]\nrequirements = [{{ name = "{PACKAGE}", specifier = "==0.3.0" }}]\n'
            'entrypoints = [{ name = "agent-bridge", install-path = "/b/agent-bridge" }]\n')

    assert upgrade.parse_receipt(text, Path("/t"), UV).kind == "uv"


def test_an_editable_install_is_moved_to_the_published_package() -> None:
    text = (f'[tool]\nrequirements = [{{ name = "{PACKAGE}", editable = "C:/src/claude remote/agent-bridge" }}]\n'
            'entrypoints = [{ name = "agent-bridge", install-path = "/b/agent-bridge" }]\n')

    install = upgrade.parse_receipt(text, Path("/t"), UV)

    assert install.kind == "source"
    assert install.source == "C:/src/claude remote/agent-bridge"
    assert install.upgrade == (UV, "tool", "install", "--reinstall", PACKAGE)
    assert install.back == 'uv tool install --reinstall --editable "C:/src/claude remote/agent-bridge"'


@pytest.mark.parametrize(("key", "value", "back"), [
    ("path", "/w/claude_remote_mcp_agent_bridge-0.4.0-py3-none-any.whl",
     "uv tool install --reinstall /w/claude_remote_mcp_agent_bridge-0.4.0-py3-none-any.whl"),
    ("directory", "/src/agent-bridge", "uv tool install --reinstall /src/agent-bridge"),
    ("git", "https://example.com/r.git", ""),
    ("url", "https://example.com/a.whl", ""),
])
def test_other_sources_are_moved_too(key: str, value: str, back: str) -> None:
    text = (f'[tool]\nrequirements = [{{ name = "{PACKAGE}", {key} = "{value}" }}]\n'
            'entrypoints = [{ name = "agent-bridge", install-path = "/b/agent-bridge" }]\n')

    install = upgrade.parse_receipt(text, Path("/t"), UV)

    assert (install.kind, install.source, install.back) == ("source", value, back)


def test_a_receipt_without_agent_bridge_is_refused() -> None:
    with pytest.raises(Refused, match="reinstall"):
        upgrade.parse_receipt('[tool]\nrequirements = [{ name = "other" }]\n', Path("/t"), UV)


def test_a_receipt_that_isnt_toml_is_refused() -> None:
    with pytest.raises(Refused, match="can't read"):
        upgrade.parse_receipt("[tool\n", Path("/t"), UV)


def test_no_uv_tool_install_is_refused(tmp_path: Path) -> None:
    with pytest.raises(Refused, match=f"uv tool install {PACKAGE}"):
        upgrade.find_install(FakeHost(tools=tmp_path / "empty"))


def test_without_uv_it_is_refused(tmp_path: Path) -> None:
    with pytest.raises(Refused, match="can't find uv"):
        upgrade.find_install(FakeHost(tools=None, uv_path=None))


def test_homebrew_on_macos_is_upgraded_with_brew(tmp_path: Path) -> None:
    host = FakeHost(tools=None, platform="darwin", installs=["Homebrew"],
                    homebrew="/opt/homebrew/opt/agent-bridge/bin/agent-bridge")

    install = upgrade.find_install(host)

    assert install.kind == "Homebrew"
    assert install.command == "/opt/homebrew/opt/agent-bridge/bin/agent-bridge"
    assert install.upgrade == (str(Path("/opt/homebrew/bin/brew")), "upgrade", "agent-bridge")


def test_two_installs_on_macos_are_refused() -> None:
    with pytest.raises(Refused, match="both Homebrew and uv"):
        upgrade.find_install(FakeHost(tools=None, platform="darwin", installs=["Homebrew", "uv"]))


def test_pipx_is_left_to_pipx() -> None:
    with pytest.raises(Refused, match=f"pipx upgrade {PACKAGE}"):
        upgrade.find_install(FakeHost(tools=None, platform="darwin", installs=["pipx"]))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd agent-bridge && uv run pytest tests/test_upgrade.py -q`
Expected: collection error, `ImportError: cannot import name 'upgrade'`.

- [ ] **Step 3: Write the module's first half**

Create `agent-bridge/src/agent_bridge/install/upgrade.py`:

```python
"""`agent-bridge upgrade`: upgrades the installed package to the latest release, then runs the
new version's setup, which restarts the service with it. A uv tool install from a checkout, a
wheel or git is moved to the published package; what it came from is left alone.

On Windows a running program's files can't be replaced, so the installed agent-bridge can't
upgrade itself. It runs from uvx's copy instead, which stops the scheduled task first:

    uvx --from claude-remote-mcp-agent-bridge@latest agent-bridge upgrade

@latest matters: without it uvx runs the installed tool's own environment.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_bridge.host import BRIDGE_VERSION
from agent_bridge.install.common import PREFIX, Refused
from agent_bridge.install.flow import FAILED, OK

__all__ = ["PACKAGE", "UVX", "Install", "RealSystem", "System", "find_install", "parse_receipt", "run", "system"]

PACKAGE = "claude-remote-mcp-agent-bridge"
UVX = f"uvx --from {PACKAGE}@latest agent-bridge upgrade"
# Where a uv tool requirement comes from when not from the package index, as uv-receipt.toml has it.
SOURCES = ("editable", "path", "directory", "git", "url")


@dataclass(frozen=True)
class Install:
    """How agent-bridge is installed: "uv" (uv tool, from the package index), "source" (uv tool,
    from a checkout, a wheel, git or a URL) or "Homebrew"."""

    kind: str
    # The installed agent-bridge, which runs setup.
    command: str
    # What upgrades it, or moves it to the published package.
    upgrade: tuple[str, ...]
    # uv's tool environment and the programs uv put on the PATH: Windows locks them while they run.
    environment: Path | None = None
    programs: tuple[str, ...] = ()
    # Where a "source" install came from, and what installs it from there again ("" when that
    # can't be told).
    source: str = ""
    back: str = ""


class System(Protocol):
    """What upgrade asks of the host; tests replace it."""

    platform: str
    # sys.prefix: the environment this agent-bridge runs from.
    prefix: Path

    # Runs from an environment in uv's cache, as uvx's.
    def in_uv_cache(self) -> bool: ...
    def uv(self) -> str | None: ...
    # `uv tool dir`; None when uv can't say.
    def tool_dir(self) -> Path | None: ...
    # How agent-bridge is installed on macOS: "Homebrew", "uv" or "pipx", any of them or none.
    def mac_installs(self) -> list[str]: ...
    def homebrew_command(self) -> str | None: ...
    # What `command --version` says, None when it doesn't run.
    def version(self, command: str) -> str | None: ...
    # Runs a command with its output shown; its exit code.
    def run(self, *argv: str) -> int: ...
    # Runs the installed agent-bridge as the service would, its output and input passed through.
    def run_installed(self, command: str, *args: str) -> int: ...
    # Windows: stops the scheduled task and makes sure nothing holds the install's files.
    def stop_bridge(self, install: Install) -> None: ...


def parse_receipt(text: str, environment: Path, uv: str) -> Install:
    """The install uv's receipt (uv-receipt.toml in the tool's environment) describes."""
    try:
        tool = tomllib.loads(text).get("tool", {})
    except tomllib.TOMLDecodeError as error:
        raise Refused(f"can't read {environment / 'uv-receipt.toml'} ({error})") from None
    requirement = next((item for item in tool.get("requirements", []) if item.get("name") == PACKAGE), None)
    programs = {item.get("name"): item.get("install-path", "") for item in tool.get("entrypoints", [])}
    command = programs.get("agent-bridge")
    if requirement is None or not command:
        raise Refused(f"{environment / 'uv-receipt.toml'} doesn't list agent-bridge: reinstall it "
                      f"(uv tool install --reinstall {PACKAGE}), then run agent-bridge setup")
    shown = tuple(path for path in programs.values() if path)
    for key in SOURCES:
        if key in requirement:
            source = str(requirement[key])
            back = {"editable": f"uv tool install --reinstall --editable {_quoted(source)}",
                    "path": f"uv tool install --reinstall {_quoted(source)}",
                    "directory": f"uv tool install --reinstall {_quoted(source)}"}.get(key, "")
            return Install("source", command, (uv, "tool", "install", "--reinstall", PACKAGE), environment, shown,
                           source, back)
    return Install("uv", command, (uv, "tool", "upgrade", PACKAGE), environment, shown)


def find_install(system: System) -> Install:
    """The one install of agent-bridge on this host, or why upgrade can't upgrade it."""
    if system.platform == "darwin":
        installs = system.mac_installs()
        if len(installs) > 1:
            raise Refused(f"agent-bridge is installed with both {' and '.join(installs)}; keep one (agent-bridge setup "
                          "says how), then run the upgrade again")
        if installs == ["pipx"]:
            raise Refused(f"agent-bridge is installed with pipx: upgrade it with pipx upgrade {PACKAGE}, then run "
                          "agent-bridge setup")
        if installs == ["Homebrew"] and (command := system.homebrew_command()):
            # <prefix>/opt/agent-bridge/bin/agent-bridge, and brew is <prefix>/bin/brew.
            brew = str(Path(command).parents[3] / "bin" / "brew")
            return Install("Homebrew", command, (brew, "upgrade", "agent-bridge"))
    uv = system.uv()
    if uv is None:
        raise Refused("can't find uv, which agent-bridge is installed with: put it on the PATH, then run the upgrade "
                      "again")
    tools = system.tool_dir()
    receipt = tools / PACKAGE / "uv-receipt.toml" if tools else None
    if receipt is None or not receipt.is_file():
        raise Refused(f"agent-bridge isn't installed with uv tool here: install it (uv tool install {PACKAGE}), "
                      "then run agent-bridge setup")
    try:
        text = receipt.read_text(encoding="utf-8")
    except OSError as error:
        raise Refused(f"can't read {receipt} ({error.strerror})") from None
    return parse_receipt(text, receipt.parent, uv)


def _quoted(text: str) -> str:
    return f'"{text}"' if " " in text else text
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd agent-bridge && uv run pytest tests/test_upgrade.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agent-bridge/src/agent_bridge/install/upgrade.py agent-bridge/tests/test_upgrade.py
git commit -m "agent-bridge: read how agent-bridge is installed, for upgrade"
```

---

### Task 2: The upgrade flow

**Files:**
- Modify: `agent-bridge/src/agent_bridge/install/upgrade.py` (append)
- Test: `agent-bridge/tests/test_upgrade.py` (append)

**Interfaces:**
- Consumes: `Install`, `find_install`, `System`, `PACKAGE`, `UVX` from Task 1; `FakeHost`, `receipt`, `host` fixture from Task 1's tests.
- Produces: `run(system: System) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `agent-bridge/tests/test_upgrade.py`:

```python
# --- Upgrading ------------------------------------------------------------------------


def test_upgrades_then_runs_the_new_setup(host: FakeHost, tmp_path: Path, capsys) -> None:
    assert upgrade.run(host) == 0

    assert host.ran("run") == [(UV, "tool", "upgrade", PACKAGE)]
    assert host.ran("installed") == [((tmp_path / "bin" / "agent-bridge").as_posix(), "setup", "0.3.1")]
    assert host.ran("stop") == []
    assert "agent-bridge 0.3.0 -> 0.3.1" in capsys.readouterr().out


def test_setups_exit_code_is_upgrades(host: FakeHost) -> None:
    host.setup_code = 3

    assert upgrade.run(host) == 3


def test_under_uvx_the_latest_release_installed_already_changes_nothing(host: FakeHost, capsys) -> None:
    host.installed = "0.3.1"

    assert upgrade.run(host) == 0

    assert host.calls == []
    assert "up to date (0.3.1)" in capsys.readouterr().out


def test_run_from_the_install_on_posix_the_version_after_decides(host: FakeHost, capsys) -> None:
    # A plain `agent-bridge upgrade`: its own version is the installed one, not the latest.
    host.uv_cache, host.installed, host.published = False, "0.3.1", "0.3.1"

    assert upgrade.run(host) == 0

    assert host.ran("run") == [(UV, "tool", "upgrade", PACKAGE)]
    assert host.ran("installed") == []
    assert "up to date (0.3.1)" in capsys.readouterr().out


def test_a_failed_upgrade_falls_back_to_a_reinstall(host: FakeHost, capsys) -> None:
    host.upgrade_code = 2

    assert upgrade.run(host) == 0

    assert host.ran("run") == [(UV, "tool", "upgrade", PACKAGE), (UV, "tool", "install", "--reinstall", PACKAGE)]
    assert host.ran("installed")[0][1:] == ("setup", "0.3.1")
    assert "reinstalling" in capsys.readouterr().err


def test_on_posix_a_failed_upgrade_leaves_the_service_as_it_was(host: FakeHost, capsys) -> None:
    host.upgrade_code = host.reinstall_code = 2

    assert upgrade.run(host) == 1

    assert host.ran("installed") == []
    assert "the upgrade failed" in capsys.readouterr().err


def test_a_package_the_failed_reinstall_removed_says_how_to_get_it_back(host: FakeHost, capsys) -> None:
    host.upgrade_code = host.reinstall_code = 2
    host.reinstall_removes = True

    assert upgrade.run(host) == 1

    assert f"uv tool install --reinstall {PACKAGE}" in capsys.readouterr().err


def test_a_checkout_install_is_moved_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(upgrade, "BRIDGE_VERSION", "0.3.1")
    receipt(tmp_path / "tools", f'{{ name = "{PACKAGE}", editable = "/src/agent-bridge" }}')
    # The same version as the latest: moved all the same, and set up again.
    host = FakeHost(tools=tmp_path / "tools", installed="0.3.1")

    assert upgrade.run(host) == 0

    assert host.ran("run") == [(UV, "tool", "install", "--reinstall", PACKAGE)]
    assert host.ran("installed")[0][1:] == ("setup", "0.3.1")
    out = capsys.readouterr().out
    assert "installed from /src/agent-bridge" in out
    assert "left as it is" in out


def test_moving_a_newer_checkout_to_the_release_warns_and_says_how_to_go_back(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(upgrade, "BRIDGE_VERSION", "0.3.1")
    receipt(tmp_path / "tools", f'{{ name = "{PACKAGE}", editable = "/src/agent-bridge" }}')
    host = FakeHost(tools=tmp_path / "tools", installed="0.4.0.dev0")

    assert upgrade.run(host) == 0

    err = capsys.readouterr().err
    assert "older than the 0.4.0.dev0" in err
    assert "uv tool install --reinstall --editable /src/agent-bridge" in err


def test_homebrew_is_upgraded_with_brew_then_set_up(capsys) -> None:
    host = FakeHost(tools=None, platform="darwin", installs=["Homebrew"], uv_cache=False,
                    homebrew="/opt/homebrew/opt/agent-bridge/bin/agent-bridge")

    assert upgrade.run(host) == 0

    assert host.ran("run") == [(str(Path("/opt/homebrew/bin/brew")), "upgrade", "agent-bridge")]
    assert host.ran("installed") == [("/opt/homebrew/opt/agent-bridge/bin/agent-bridge", "setup", "0.3.1")]


def test_other_platforms_are_refused(host: FakeHost, capsys) -> None:
    host.platform = "freebsd14"

    assert upgrade.run(host) == 1

    assert "macOS, Linux and Windows" in capsys.readouterr().err


# --- Windows --------------------------------------------------------------------------


def test_on_windows_the_installed_agent_bridge_points_at_uvx(host: FakeHost, capsys) -> None:
    host.platform, host.uv_cache = "win32", False
    host.prefix = host.tools / PACKAGE

    assert upgrade.run(host) == 1

    assert host.calls == []
    assert upgrade.UVX in capsys.readouterr().err


def test_on_windows_the_task_is_stopped_before_uv_runs(host: FakeHost, tmp_path: Path, capsys) -> None:
    host.platform = "win32"

    assert upgrade.run(host) == 0

    assert [call[0] for call in host.calls] == ["stop", "run", "installed"]
    assert host.ran("stop")[0][0] == str(tmp_path / "tools" / PACKAGE)
    assert "the gateway can't reach them meanwhile" in capsys.readouterr().out


def test_on_windows_setup_runs_even_when_the_version_didnt_change(host: FakeHost) -> None:
    # From a checkout's .venv: not uvx, so its version isn't the latest's.
    host.platform, host.uv_cache, host.installed, host.published = "win32", False, "0.3.1", "0.3.1"

    assert upgrade.run(host) == 0

    assert host.ran("installed")[0][1:] == ("setup", "0.3.1")


def test_on_windows_a_refused_stop_changes_nothing(host: FakeHost, capsys) -> None:
    host.platform, host.stop_refusal = "win32", "agent-bridge.exe (PID 5000) still runs"

    assert upgrade.run(host) == 1

    assert host.ran("run") == []
    assert "PID 5000" in capsys.readouterr().err


def test_on_windows_a_failed_upgrade_sets_the_old_version_up_again(host: FakeHost, capsys) -> None:
    host.platform = "win32"
    host.upgrade_code = host.reinstall_code = 2

    assert upgrade.run(host) == 1

    assert host.ran("installed")[0][1:] == ("setup", "0.3.0")
    assert "the version still installed" in capsys.readouterr().out


def test_on_windows_a_removed_package_stays_stopped_with_how_to_recover(host: FakeHost, capsys) -> None:
    host.platform = "win32"
    host.upgrade_code = host.reinstall_code = 2
    host.reinstall_removes = True

    assert upgrade.run(host) == 1

    assert host.ran("installed") == []
    err = capsys.readouterr().err
    assert f"uv tool install --reinstall {PACKAGE}" in err
    assert "agent-bridge setup" in err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd agent-bridge && uv run pytest tests/test_upgrade.py -q`
Expected: the new tests fail with `AttributeError: module 'agent_bridge.install.upgrade' has no attribute 'run'`.

- [ ] **Step 3: Implement the flow**

Insert into `upgrade.py`, after `find_install` (before `_quoted`):

```python
def run(system: System) -> int:
    try:
        return _upgrade(system)
    except Refused as error:
        print(f"error: {error}", file=sys.stderr)
        return FAILED


def _upgrade(system: System) -> int:
    if system.platform not in ("win32", "darwin") and not system.platform.startswith("linux"):
        raise Refused("agent-bridge upgrade is for macOS, Linux and Windows")
    install = find_install(system)
    windows = system.platform == "win32"
    if windows and install.environment is not None and _same(system.prefix, install.environment):
        raise Refused("Windows can't replace the files of a running program, so this agent-bridge can't upgrade "
                      f"itself. Run the upgrade from uvx's copy, outside the install:\n  {UVX}")
    before = system.version(install.command)
    # uvx@latest runs the latest release: nothing to do, and nothing to stop.
    if system.in_uv_cache() and install.kind == "uv" and before == BRIDGE_VERSION:
        print(f"agent-bridge is up to date ({before}).")
        return OK
    if install.kind == "source":
        print(f"agent-bridge {before or '(version unknown)'} is installed from {install.source}; replacing it with "
              "the published package. What it was installed from is left as it is.")
    if windows:
        print("agent-bridge stops until setup starts it again. Agents keep running in herdr, but the gateway can't "
              "reach them meanwhile, also an agent that runs this upgrade.")
        system.stop_bridge(install)
    upgraded = system.run(*install.upgrade) == 0
    if not upgraded and install.kind == "uv":
        print("warning: uv tool upgrade failed; reinstalling the package instead", file=sys.stderr)
        upgraded = system.run(*install.upgrade[:2], "install", "--reinstall", PACKAGE) == 0
    after = system.version(install.command)
    if not upgraded or after is None:
        return _failed(system, install, windows, after)
    if install.kind != "source" and after == before:
        print(f"agent-bridge is up to date ({after}).")
        # On Windows it was stopped, and setup starts it again.
        if not windows:
            return OK
    else:
        print(f"agent-bridge {before or '(version unknown)'} -> {after}")
    if install.kind == "source" and before and _release(after) < _release(before):
        back = install.back or f"reinstall it from {install.source}"
        print(f"warning: that is older than the {before} you had. To go back: {back}, then agent-bridge setup",
              file=sys.stderr)
    return system.run_installed(install.command, "setup")


def _failed(system: System, install: Install, windows: bool, after: str | None) -> int:
    print("error: the upgrade failed (see above).", file=sys.stderr)
    if after is None:
        reinstall = "brew reinstall agent-bridge" if install.kind == "Homebrew" else f"uv tool install --reinstall {PACKAGE}"
        stopped = "agent-bridge is gone, and stays stopped" if windows else "agent-bridge is gone"
        print(f"{stopped}. Install it again, then set it up:\n  {reinstall}\n  agent-bridge setup", file=sys.stderr)
        return FAILED
    if windows:
        # Stopped above: the version still there is set up again rather than left off.
        print(f"Setting up agent-bridge {after} again, the version still installed.")
        system.run_installed(install.command, "setup")
    return FAILED


def _same(one: Path, other: Path) -> bool:
    return os.path.normcase(os.path.realpath(one)) == os.path.normcase(os.path.realpath(other))


def _release(version: str) -> tuple[int, ...]:
    """A version's release numbers, e.g. (0, 4, 0) for 0.4.0.dev0."""
    match = re.match(r"\d+(?:\.\d+)*", version)
    return tuple(int(part) for part in match.group().split(".")) if match else ()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd agent-bridge && uv run pytest tests/test_upgrade.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agent-bridge/src/agent_bridge/install/upgrade.py agent-bridge/tests/test_upgrade.py
git commit -m "agent-bridge: upgrade the package and run the new setup"
```

---

### Task 3: Stopping the Windows task for an upgrade

**Files:**
- Modify: `agent-bridge/src/agent_bridge/install/windows.py` (add after `uninstall`, ~line 337; add `"stop_for_upgrade"` to `__all__`)
- Test: `agent-bridge/tests/test_install_windows.py` (append)

**Interfaces:**
- Consumes: `WindowsService.prepare()`, `WindowsService._stop(ports)`, `WindowsService.abandon()`, `_other_account(system)`, `System.processes()`, `Listener(pid, name, command_line, path)`.
- Produces: `stop_for_upgrade(system: System, environment: Path | None, programs: Iterable[str]) -> None` (raises `Refused`).

- [ ] **Step 1: Write the failing tests**

Append to `agent-bridge/tests/test_install_windows.py` (it already has `host`, `setup`, `every_flag`, `LAUNCHER`, `SERVER`, `Listener`, `Refused`):

```python
# --- Stopping for agent-bridge upgrade -------------------------------------------------


def upgrade_paths(host: FakeWindows) -> tuple[Path, list[str]]:
    """uv's tool environment, and the programs uv put on the PATH (the task runs agent-bridgew)."""
    return host.home / "uv" / "tools" / "claude-remote-mcp-agent-bridge", [
        str(host.bin / "agent-bridge.exe"), str(host.bin / "agent-bridgew.exe")]


def test_upgrade_stops_the_task_and_its_bridge_and_leaves_it_disabled(host: FakeWindows, capsys) -> None:
    assert setup(host, capsys, *every_flag(host)).code == 0

    windows.stop_for_upgrade(host, *upgrade_paths(host))

    assert not host.task_on
    assert not {LAUNCHER, SERVER} & host.pids()
    # Setup, run after the upgrade, brings it back.
    assert host.paths.disabled_marker.exists()


def test_upgrade_with_a_program_still_running_from_the_install_puts_the_task_back(host: FakeWindows, capsys) -> None:
    assert setup(host, capsys, *every_flag(host)).code == 0
    environment, programs = upgrade_paths(host)
    host.processes_ += [
        (Listener(5000, "agent-bridge.exe", f'"{programs[0]}" check', programs[0]), 0),
        (Listener(5001, "python.exe", "python.exe -c pass", str(environment / "Scripts" / "python.exe")), 0),
    ]

    with pytest.raises(Refused, match=r"agent-bridge.exe \(PID 5000\), python.exe \(PID 5001\)"):
        windows.stop_for_upgrade(host, environment, programs)

    assert host.task_on
    assert SERVER in host.pids()
    assert not host.paths.disabled_marker.exists()


def test_upgrade_ignores_programs_elsewhere(host: FakeWindows, capsys) -> None:
    assert setup(host, capsys, *every_flag(host)).code == 0
    # uvx's copy, which runs the upgrade, and another environment's python.
    host.processes_ += [
        (Listener(6000, "agent-bridge.exe", "agent-bridge.exe upgrade", r"C:\cache\archive-v0\x\Scripts\agent-bridge.exe"), 0),
        (Listener(6001, "python.exe", "python.exe", r"C:\Python\python.exe"), 0),
    ]

    windows.stop_for_upgrade(host, *upgrade_paths(host))

    assert not host.task_on


def test_upgrade_on_a_host_never_set_up_stops_nothing(host: FakeWindows) -> None:
    windows.stop_for_upgrade(host, *upgrade_paths(host))

    assert host.ran("schtasks") == []


def test_upgrade_as_another_account_than_the_desktops_is_refused(host: FakeWindows) -> None:
    host.desktop = "PC\\someone"

    with pytest.raises(Refused, match="run this as PC\\\\someone"):
        windows.stop_for_upgrade(host, *upgrade_paths(host))

    assert host.ran("schtasks") == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd agent-bridge && uv run pytest tests/test_install_windows.py -q -k upgrade`
Expected: FAIL with `AttributeError: module 'agent_bridge.install.windows' has no attribute 'stop_for_upgrade'`.

- [ ] **Step 3: Implement `stop_for_upgrade`**

In `windows.py`, add `"stop_for_upgrade"` to `__all__` (keep it sorted), and add after `uninstall`:

```python
def stop_for_upgrade(system: System, environment: Path | None, programs: Iterable[str]) -> None:
    """For `agent-bridge upgrade`: stops the task and the bridge it runs, as setup does, leaving
    the task disabled for the setup that follows to bring back. Refused, with the task put back,
    while anything else still runs from the install (e.g. an agent-bridge check in another
    terminal): uv couldn't replace its files, and would leave the package half removed."""
    if (refusal := _other_account(system)) is not None:
        raise Refused(refusal)
    service = WindowsService(system, lambda: None)
    service.prepare()
    if service.previous is not None:
        print(f"\n==> Stopping the scheduled task {TASK}")
    service._stop((service.installed_port,))
    held = [process for process in system.processes() if _runs_from(process, environment, programs)]
    if held:
        service.abandon()
        running = ", ".join(f"{process.name} (PID {process.pid})" for process in held)
        raise Refused(f"{running} still {'run' if len(held) > 1 else 'runs'} from the installed agent-bridge, so uv "
                      "couldn't replace its files. Close them, then run the upgrade again; agent-bridge was started "
                      "again meanwhile")


def _runs_from(process: Listener, environment: Path | None, programs: Iterable[str]) -> bool:
    """Whether `process` is one of `programs` or runs from `environment`."""
    path = os.path.normcase(process.path)
    if not path:
        return False
    if environment is not None and path.startswith(os.path.normcase(str(environment)) + os.sep):
        return True
    return any(path == os.path.normcase(program) for program in programs)
```

Note: `os.path.normcase` lowercases and turns `/` into `\` on Windows only. The tests build every path with `pathlib`/`str()` on the host they run on, so the comparison works on any OS.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd agent-bridge && uv run pytest tests/test_install_windows.py -q`
Expected: all pass (the earlier tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add agent-bridge/src/agent_bridge/install/windows.py agent-bridge/tests/test_install_windows.py
git commit -m "agent-bridge: stop the Windows task for an upgrade, and refuse while the install is in use"
```

---

### Task 4: The real system and the `upgrade` command

**Files:**
- Modify: `agent-bridge/src/agent_bridge/install/upgrade.py` (append `RealSystem`, `system`, `_environment`)
- Modify: `agent-bridge/src/agent_bridge/cli.py` (docstring line 1, `main`, `_parser`, new `_upgrade`)
- Test: `agent-bridge/tests/test_upgrade.py` (append)

**Interfaces:**
- Consumes: `run`, `Install`, `System` (Tasks 1–2); `windows.stop_for_upgrade` (Task 3); `macos.MacSystem.installs()` / `MacSystem.homebrew_commands`.
- Produces: `RealSystem`, `system() -> RealSystem`; CLI `agent-bridge upgrade`.

- [ ] **Step 1: Write the failing tests**

Append to `agent-bridge/tests/test_upgrade.py`:

```python
# --- The command and the real system --------------------------------------------------


def test_the_command_runs_the_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_bridge import cli

    ran = []
    monkeypatch.setattr(upgrade, "system", lambda: "the host")
    monkeypatch.setattr(upgrade, "run", lambda system: ran.append(system) or 7)

    assert cli.main(["upgrade"]) == 7
    assert ran == ["the host"]


def test_the_real_system_reads_the_version_agent_bridge_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def fake_run(argv, **options):
        seen.update(argv=argv, env=options["env"])
        return type("Result", (), {"returncode": 0, "stdout": "agent-bridge 0.3.1\n"})()

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    monkeypatch.setenv("AGENT_BRIDGE_TOKEN", "secret")

    assert upgrade.RealSystem().version("/b/agent-bridge") == "0.3.1"
    assert seen["argv"] == ["/b/agent-bridge", "--version"]
    assert "AGENT_BRIDGE_TOKEN" not in seen["env"]


def test_the_real_system_finds_uv_where_uv_says(monkeypatch: pytest.MonkeyPatch) -> None:
    # uv tells the programs it runs where it is.
    monkeypatch.setenv("UV", "/opt/uv/bin/uv")

    assert upgrade.RealSystem().uv() == "/opt/uv/bin/uv"


def test_uvx_environments_are_in_uvs_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    real = upgrade.RealSystem()
    real.prefix = Path("/home/me/.cache/uv/archive-v0/abc")
    assert real.in_uv_cache()
    real.prefix = Path("/home/me/.local/share/uv/tools") / PACKAGE
    assert not real.in_uv_cache()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd agent-bridge && uv run pytest tests/test_upgrade.py -q`
Expected: the new tests fail (`invalid choice: 'upgrade'` from argparse, exit 2 as `SystemExit`; `AttributeError` for `RealSystem`).

- [ ] **Step 3: Implement `RealSystem`**

Append to `upgrade.py`:

```python
class RealSystem:
    """What upgrade asks of the real host."""

    def __init__(self) -> None:
        self.platform = sys.platform
        self.prefix = Path(sys.prefix)

    def in_uv_cache(self) -> bool:
        # uvx's environments are <uv cache>/archive-v0/<id>.
        return self.prefix.parent.name.startswith("archive-v")

    def uv(self) -> str | None:
        # uv tells the programs it runs where it is.
        return os.environ.get("UV") or shutil.which("uv")

    def tool_dir(self) -> Path | None:
        uv = self.uv()
        if uv is None:
            return None
        try:
            result = subprocess.run([uv, "tool", "dir"], capture_output=True, text=True, timeout=60,
                                    stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None
        found = result.stdout.strip()
        return Path(found) if result.returncode == 0 and found else None

    def mac_installs(self) -> list[str]:
        from agent_bridge.install import macos

        return macos.MacSystem().installs()

    def homebrew_command(self) -> str | None:
        from agent_bridge.install import macos

        return next((command for command in macos.MacSystem.homebrew_commands if os.path.exists(command)), None)

    def version(self, command: str) -> str | None:
        try:
            result = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=60,
                                    stdin=subprocess.DEVNULL, env=_environment())
        except (OSError, subprocess.TimeoutExpired):
            return None
        found = re.search(r"agent-bridge (\S+)", result.stdout or "")
        return found.group(1) if result.returncode == 0 and found else None

    def run(self, *argv: str) -> int:
        # What was printed so far goes first: piped, Python holds it while the child writes.
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            return subprocess.run(argv).returncode
        except OSError as error:
            print(f"error: can't run {argv[0]} ({error.strerror})", file=sys.stderr)
            return FAILED

    def run_installed(self, command: str, *args: str) -> int:
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            return subprocess.run([command, *args], env=_environment(), cwd=tempfile.gettempdir()).returncode
        except OSError as error:
            raise Refused(f"can't run {command} ({error.strerror})") from None

    def stop_bridge(self, install: Install) -> None:
        from agent_bridge.install import windows

        windows.stop_for_upgrade(windows.system(), install.environment, install.programs)


def system() -> RealSystem:
    return RealSystem()


def _environment() -> dict[str, str]:
    """This environment without AGENT_BRIDGE_*, which would win over the env file, as setup runs
    agent-bridge; in UTF-8 mode, as Windows' code page may not hold what it prints."""
    environment = {name: value for name, value in os.environ.items() if not name.upper().startswith(PREFIX)}
    environment["PYTHONUTF8"] = "1"
    return environment
```

- [ ] **Step 4: Wire the CLI**

In `cli.py`:

1. First docstring line becomes:
   ```python
   """`agent-bridge serve | enroll | rename | unenroll | save-agents | check | setup | upgrade | uninstall | start-herdr`:
   ```
2. In `main`, after the `setup` branch:
   ```python
        if args.command == "upgrade":
            return _upgrade()
   ```
3. In `_parser`, after the `setup` arguments (before `commands.add_parser("uninstall", …)`):
   ```python
    commands.add_parser(
        "upgrade", help="upgrade the installed package to the latest release and run its setup again; on Windows, "
                        "run it as uvx --from claude-remote-mcp-agent-bridge@latest agent-bridge upgrade",
    )
   ```
4. After `_setup`:
   ```python
   def _upgrade() -> int:
       from agent_bridge.install import upgrade

       return upgrade.run(upgrade.system())
   ```

- [ ] **Step 5: Run the whole suite**

Run: `cd agent-bridge && uv run pytest -q`
Expected: all pass (763 before this work, plus the new tests).

- [ ] **Step 6: Commit**

```bash
git add agent-bridge/src/agent_bridge/install/upgrade.py agent-bridge/src/agent_bridge/cli.py agent-bridge/tests/test_upgrade.py
git commit -m "agent-bridge: the upgrade command"
```

---

### Task 5: README

**Files:**
- Modify: `agent-bridge/README.md` (macOS ~line 483, Linux ~line 678, Windows ~lines 787–818)

- [ ] **Step 1: Windows section.** Replace the paragraph starting "Windows can't replace files a running program has open…" through the "If an upgrade failed that way…" paragraph (~lines 792–804) with:

````markdown
To upgrade, run agent-bridge's upgrade from uvx's copy of the latest release. Windows can't
replace the files of a running program, so the installed agent-bridge can't upgrade itself:

```powershell
uvx --from claude-remote-mcp-agent-bridge@latest agent-bridge upgrade
```

It stops the task, upgrades the package (`uv tool upgrade`, else a reinstall) and runs the
new `agent-bridge setup`, which starts it again; the agents keep running in herdr, but the
gateway can't reach them until then. It refuses, with agent-bridge started again, while
another program still runs from the install (an `agent-bridge check` in another terminal).
If the upgrade fails and the old version is still there, it sets that one up again. Keep
`@latest`: without it uvx runs the installed copy, which can't replace itself. To let an
agent upgrade the host, allow it exactly that command.

By hand, stop agent-bridge first, or `uv tool upgrade` fails while it runs and leaves the
package half removed:

```powershell
schtasks /Change /TN agent-bridge /DISABLE; schtasks /End /TN agent-bridge
uv tool upgrade claude-remote-mcp-agent-bridge
agent-bridge setup
```

From Git Bash (as Claude Code's `!` runs commands), prefix the schtasks commands with
`MSYS_NO_PATHCONV=1`. If an upgrade failed that way, `agent-bridge` is gone too: run
`uv tool install --reinstall claude-remote-mcp-agent-bridge` (with agent-bridge stopped,
as above), then `agent-bridge setup`.
````

- [ ] **Step 2: Windows checkout paragraph.** Replace "To move it to the published package, stop agent-bridge (as above), run `uv tool install --reinstall claude-remote-mcp-agent-bridge`, then `agent-bridge setup`." (~lines 816–818) with:

```markdown
`agent-bridge upgrade` (run with uvx, as above) moves it to the published package: it says
what it replaces and, when the release is older than what ran, how to go back. The checkout
itself is left as it is.
```

- [ ] **Step 3: macOS section.** In the paragraph at ~line 483, replace "Re-run `agent-bridge setup` after upgrading, which restarts the service." with:

```markdown
`agent-bridge upgrade` does both and runs setup again, which restarts the service (or run
it as `uvx --from claude-remote-mcp-agent-bridge@latest agent-bridge upgrade`). By hand,
re-run `agent-bridge setup` after upgrading.
```

And in the checkout paragraph at ~line 513, replace "To move it to the published package, run `uv tool install --reinstall claude-remote-mcp-agent-bridge`, then `agent-bridge setup`." with "`agent-bridge upgrade` moves it to the published package (the checkout is left as it is), or by hand: `uv tool install --reinstall claude-remote-mcp-agent-bridge`, then `agent-bridge setup`."

- [ ] **Step 4: Linux section.** At ~line 678 replace "re-run `agent-bridge setup` after upgrading, which restarts agent-bridge but not herdr's server." with:

```markdown
`agent-bridge upgrade` upgrades it and runs setup again, which restarts agent-bridge but
not herdr's server (by hand: `uv tool upgrade`, then `agent-bridge setup`).
```

And the checkout paragraph at ~line 694 the same way as macOS (Step 3, second replacement).

- [ ] **Step 5: Check the README renders sensibly**

Run: `cd agent-bridge && grep -n "agent-bridge upgrade" README.md`
Expected: hits in the macOS, Linux and Windows sections; no leftover sentence that tells Windows users only the manual steps.

- [ ] **Step 6: Commit**

```bash
git add agent-bridge/README.md
git commit -m "agent-bridge: document agent-bridge upgrade"
```

---

### Task 6: Try it on this Windows host

Manual check, after Tasks 1–5; not a code change.

- [ ] **Step 1:** The published 0.3.0 has no `upgrade`, so uvx can't run it yet. Run the branch's code from the checkout's `.venv` instead: like uvx's copy, it is outside the tool environment, so it exercises the same Windows path against the real install.

Run: `cd agent-bridge && uv run agent-bridge upgrade`
Expected: since the checkout's version (0.3.0) equals the installed one and it's not uvx, it stops the task, runs `uv tool upgrade` ("Nothing to upgrade"), prints `agent-bridge is up to date (0.3.0).`, runs the installed `agent-bridge setup`, and `agent-bridge check` then passes.

- [ ] **Step 2:** `agent-bridge check` — every line `ok`.

Ask the user before Step 1: it stops this host's bridge briefly.
