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
    # Raised by stop_bridge, as Ctrl+C while it waits for the bridge to end does.
    stop_error: BaseException | None = None
    # Windows: an earlier upgrade stopped the task and was cut short before its setup.
    stopped_before: bool = False
    setup_code: int = 0
    # Raised by run_installed / run, as a setup that can't start or Ctrl+C does.
    setup_error: BaseException | None = None
    run_error: BaseException | None = None
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
        if self.run_error:
            raise self.run_error
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
        if self.setup_error:
            raise self.setup_error
        return self.setup_code

    def stop_bridge(self, install: upgrade.Install) -> None:
        self.calls.append(("stop", str(install.environment), *install.programs))
        if self.stop_error:
            raise self.stop_error
        if self.stop_refusal:
            raise Refused(self.stop_refusal)

    def left_stopped(self) -> bool:
        return self.stopped_before

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


def test_without_uv_it_is_refused() -> None:
    with pytest.raises(Refused, match="can't find uv"):
        upgrade.find_install(FakeHost(tools=None, uv_path=None))


def test_homebrew_on_macos_is_upgraded_with_brew() -> None:
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


def test_on_windows_after_an_upgrade_cut_short_the_latest_installed_is_set_up_again(host: FakeHost, capsys) -> None:
    # The first run's uv upgraded it, then it was closed before its setup started the task again.
    host.platform, host.installed, host.published, host.stopped_before = "win32", "0.3.1", "0.3.1", True

    assert upgrade.run(host) == 0

    assert host.ran("stop")
    assert host.ran("installed")[0][1:] == ("setup", "0.3.1")


def test_on_windows_the_latest_installed_with_nothing_left_stopped_changes_nothing(host: FakeHost) -> None:
    host.platform, host.installed = "win32", "0.3.1"

    assert upgrade.run(host) == 0

    assert host.calls == []


def test_an_upgrade_whose_new_version_doesnt_answer_is_set_up_all_the_same(host: FakeHost, capsys) -> None:
    # uv installed it, but `agent-bridge --version` timed out: setup says if something is wrong.
    host.published, host.setup_code = None, 4

    assert upgrade.run(host) == 4

    assert host.ran("installed")[0][1] == "setup"
    err = capsys.readouterr().err
    assert "doesn't say its version" in err
    assert "the upgrade failed" not in err


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
    assert "uv tool install --reinstall --editable /src/agent-bridge, then agent-bridge setup" in err


def test_on_windows_the_way_back_from_a_downgrade_says_to_stop_agent_bridge_first(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(upgrade, "BRIDGE_VERSION", "0.3.1")
    receipt(tmp_path / "tools", f'{{ name = "{PACKAGE}", editable = "/src/agent-bridge" }}')
    host = FakeHost(tools=tmp_path / "tools", installed="0.4.0.dev0", platform="win32")

    assert upgrade.run(host) == 0

    assert "--editable /src/agent-bridge with agent-bridge stopped first" in capsys.readouterr().err


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


class FakeSetup:
    """A started agent-bridge setup: wait() raises KeyboardInterrupt for each Ctrl+C in `interrupts`,
    then returns `code`."""

    def __init__(self, seen: dict, code: int = 0, interrupts: int = 0) -> None:
        self.seen, self.code, self.interrupts = seen, code, interrupts
        self.killed = False

    def __call__(self, argv, **options):
        self.seen.update(options, argv=argv)
        return self

    def wait(self) -> int:
        if self.interrupts:
            self.interrupts -= 1
            raise KeyboardInterrupt
        return self.code

    def kill(self) -> None:
        self.killed = True


@pytest.mark.parametrize(("platform", "cwd"), [("darwin", "/"), ("linux", "/")])
def test_the_new_setup_runs_from_the_root_on_posix(monkeypatch: pytest.MonkeyPatch, platform: str, cwd: str) -> None:
    # Not the shared /tmp, where another user could plant the legacy .env agent-bridge reads.
    seen = {}
    monkeypatch.setattr(upgrade.subprocess, "Popen", FakeSetup(seen))
    real = upgrade.RealSystem()
    real.platform = platform

    assert real.run_installed("/b/agent-bridge", "setup") == 0
    assert seen["cwd"] == cwd
    assert seen["argv"] == ["/b/agent-bridge", "setup"]


def test_the_new_setup_runs_from_the_temp_folder_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}
    monkeypatch.setattr(upgrade.subprocess, "Popen", FakeSetup(seen))
    real = upgrade.RealSystem()
    real.platform = "win32"

    real.run_installed("C:/b/agent-bridge.exe", "setup")

    assert seen["cwd"] == upgrade.tempfile.gettempdir()


def test_ctrl_c_lets_the_new_setup_finish_putting_things_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ctrl+C reaches setup too; it is waited for, not killed, and its exit code is upgrade's.
    setup = FakeSetup({}, code=130, interrupts=2)
    monkeypatch.setattr(upgrade.subprocess, "Popen", setup)

    assert upgrade.RealSystem().run_installed("/b/agent-bridge", "setup") == 130
    assert not setup.killed


def test_a_setup_that_cant_start_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(argv, **options):
        raise FileNotFoundError(2, "No such file")

    monkeypatch.setattr(upgrade.subprocess, "Popen", fail)

    with pytest.raises(Refused, match=r"can't run /b/agent-bridge \(No such file\)"):
        upgrade.RealSystem().run_installed("/b/agent-bridge", "setup")


def test_a_receipt_with_entries_that_arent_tables_is_refused() -> None:
    with pytest.raises(Refused, match="doesn't list agent-bridge"):
        upgrade.parse_receipt('[tool]\nrequirements = ["x"]\nentrypoints = ["y"]\n', Path("/t"), UV)


def test_on_windows_a_setup_that_cant_run_says_how_to_start_agent_bridge(host: FakeHost, capsys) -> None:
    host.platform, host.setup_error = "win32", Refused("can't run agent-bridge (No such file)")

    assert upgrade.run(host) == 1

    err = capsys.readouterr().err
    assert "can't run agent-bridge" in err
    assert "agent-bridge setup" in err


def test_on_windows_ctrl_c_during_the_upgrade_says_how_to_start_agent_bridge(host: FakeHost, capsys) -> None:
    host.platform, host.run_error = "win32", KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        upgrade.run(host)

    err = capsys.readouterr().err
    assert "agent-bridge setup" in err
    assert f"uv tool install --reinstall {PACKAGE}" in err


def test_on_windows_ctrl_c_while_stopping_says_how_to_start_agent_bridge(host: FakeHost, capsys) -> None:
    host.platform, host.stop_error = "win32", KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        upgrade.run(host)

    assert host.ran("run") == []
    assert "agent-bridge setup" in capsys.readouterr().err


def test_a_pinned_install_that_uv_keeps_back_isnt_called_up_to_date(host: FakeHost, capsys) -> None:
    # uv tool upgrade keeps a pin (and says so) and exits 0; uvx@latest runs 0.3.1.
    host.published = "0.3.0"

    assert upgrade.run(host) == 0

    out = capsys.readouterr().out
    assert "up to date" not in out
    assert "agent-bridge stays at 0.3.0; the latest is 0.3.1" in out
    assert host.ran("installed") == []


def test_on_posix_ctrl_c_during_the_upgrade_adds_nothing(host: FakeHost, capsys) -> None:
    host.run_error = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        upgrade.run(host)

    assert "agent-bridge setup" not in capsys.readouterr().err


def test_the_real_system_tells_a_task_left_stopped_by_setups_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_bridge.install import windows

    paths = windows.Paths(tmp_path / "Roaming", tmp_path / "Local")
    monkeypatch.setattr(windows, "system", lambda: type("Host", (), {"paths": paths})())
    real = upgrade.RealSystem()
    real.platform = "win32"

    assert not real.left_stopped()
    paths.disabled_marker.parent.mkdir(parents=True)
    paths.disabled_marker.write_text("setup\n", encoding="utf-8")
    assert real.left_stopped()
    real.platform = "linux"
    assert not real.left_stopped()
