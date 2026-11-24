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
