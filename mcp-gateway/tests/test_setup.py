"""`mcp-gateway --version` and `mcp-gateway setup`: the shipped script gets the exact
packages of the environment setup runs from, to install for the service."""

import json
import os
import subprocess
import sys
import tomllib
import zipfile
from importlib.metadata import distributions
from pathlib import Path

import pytest

from mcp_gateway import cli

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"
SCRIPT = Path(cli.__file__).parent / "deploy" / "setup.sh"


def test_version_is_the_packages(capsys: pytest.CaptureFixture) -> None:
    version = tomllib.loads(PYPROJECT.read_text())["project"]["version"]

    with pytest.raises(SystemExit) as exit_:
        cli.main(["--version"])

    assert exit_.value.code == 0
    assert capsys.readouterr().out == f"mcp-gateway {version}\n"


class Site:
    """A site-packages folder with the dist-info folders of the packages given."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir()

    def add(self, name: str, version: str, direct_url: dict | None = None) -> None:
        info = self.path / f"{name.replace('-', '_')}-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        if direct_url is not None:
            (info / "direct_url.json").write_text(json.dumps(direct_url))


class Setup:
    def __init__(self, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch):
        self.site = Site(tmp_path / "site-packages")
        self.capsys = capsys
        self.runs: list[list[str]] = []
        self.code = 0
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        for name in cli.SETUP_VARIABLES:
            monkeypatch.delenv(name, raising=False)

    def run(self, command: list[str]) -> int:
        self.runs.append(command)
        return self.code

    def __call__(self) -> tuple[int, str]:
        code = cli.main(["setup"], run=self.run, site_packages=lambda: [str(self.site.path)])
        out, err = self.capsys.readouterr()
        return code, out + err


@pytest.fixture
def setup(tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> Setup:
    return Setup(tmp_path, capsys, monkeypatch)


def test_setup_runs_the_shipped_script_with_sudo_on_the_exact_packages(setup: Setup) -> None:
    setup.site.add("claude-remote-mcp-gateway", "0.3.0")
    setup.site.add("httpx", "0.28.1")

    assert setup() == (0, "")

    (command,) = setup.runs
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert command[:2] == ["sudo", f"--preserve-env={','.join(cli.SETUP_VARIABLES)}"]
    assert command[2:] == ["bash", str(SCRIPT), python, "claude-remote-mcp-gateway==0.3.0", "httpx==0.28.1"]
    assert SCRIPT.is_file()


def test_setup_passes_on_every_variable_the_script_reads() -> None:
    # Each one sudo would otherwise drop, since the script reads it from its environment.
    used = {name for name in cli.SETUP_VARIABLES if f"${{{name}" in SCRIPT.read_text()}
    assert used == set(cli.SETUP_VARIABLES)
    for name in ("PUBLIC_HOST", "AGENT_BRIDGE_TOKEN", "MCP_GATEWAY_OAUTH_PASSPHRASE", "PORT", "TUNNEL_NAME"):
        assert name in cli.SETUP_VARIABLES


def test_setup_says_which_variables_it_passes_on_without_their_secrets(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One exported for something else, e.g. PORT for a dev server, would move the service.
    monkeypatch.setenv("PORT", "3000")
    monkeypatch.setenv("AGENT_BRIDGE_TOKEN", "t" * 32)
    monkeypatch.setenv("TUNNEL_NAME", "")
    setup.site.add("claude-remote-mcp-gateway", "0.3.0")

    code, output = setup()

    assert code == 0
    assert output == "Passing on from your environment: PORT=3000, AGENT_BRIDGE_TOKEN\n"


def test_setup_run_as_root_runs_the_script_without_sudo(setup: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    setup.site.add("claude-remote-mcp-gateway", "0.3.0")

    assert setup()[0] == 0
    assert setup.runs[0][:2] == ["bash", str(SCRIPT)]


def test_setup_exits_with_the_scripts_code(setup: Setup) -> None:
    setup.site.add("claude-remote-mcp-gateway", "0.3.0")
    setup.code = 1

    assert setup()[0] == 1


@pytest.mark.parametrize(("direct_url", "requirement"), [
    ({"url": "file:///home/me/dist/gw-0.3.0-py3-none-any.whl", "archive_info": {}},
     "claude-remote-mcp-gateway @ file:///home/me/dist/gw-0.3.0-py3-none-any.whl"),
    ({"url": "file:///home/me/claude-remote-mcp/mcp-gateway", "dir_info": {}},
     "claude-remote-mcp-gateway @ file:///home/me/claude-remote-mcp/mcp-gateway"),
    ({"url": "https://github.com/placheckij/claude-remote-mcp", "subdirectory": "mcp-gateway",
      "vcs_info": {"vcs": "git", "requested_revision": "main", "commit_id": "a" * 40}},
     f"claude-remote-mcp-gateway @ git+https://github.com/placheckij/claude-remote-mcp@{'a' * 40}"
     "#subdirectory=mcp-gateway"),
])
def test_setup_installs_a_build_from_elsewhere_from_the_same_place(setup: Setup, direct_url: dict,
                                                                   requirement: str) -> None:
    setup.site.add("claude-remote-mcp-gateway", "0.3.0", direct_url)

    assert setup()[0] == 0
    assert setup.runs[0][-1] == requirement


def test_setup_refuses_an_editable_install(setup: Setup) -> None:
    # The service can't read the user's home folder (ProtectHome=yes), where its code would stay.
    setup.site.add("claude-remote-mcp-gateway", "0.3.0",
                   {"url": "file:///home/me/claude-remote-mcp/mcp-gateway", "dir_info": {"editable": True}})

    code, output = setup()

    assert code == 1 and setup.runs == []
    assert "uv tool install /home/me/claude-remote-mcp/mcp-gateway" in output


def test_setup_refuses_where_there_is_no_mcp_gateway_package(setup: Setup) -> None:
    setup.site.add("httpx", "0.28.1")

    code, output = setup()

    assert code == 1 and setup.runs == []
    assert "uv tool install claude-remote-mcp-gateway" in output


def test_the_packages_come_from_the_running_environment() -> None:
    names = {cli._name(dist) for dist in distributions(path=cli._site_packages())}

    assert {"claude-remote-mcp-gateway", "httpx", "uvicorn"} <= names


def test_setup_is_for_linux_only(setup: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    setup.site.add("claude-remote-mcp-gateway", "0.3.0")

    code, output = setup()

    assert code == 1 and setup.runs == []
    assert "Linux" in output


def test_the_shipped_script_is_in_the_wheel(tmp_path: Path) -> None:
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(PYPROJECT.parent)],
                   check=True, capture_output=True)
    (wheel,) = tmp_path.glob("claude_remote_mcp_gateway-*.whl")
    assert "mcp_gateway/deploy/setup.sh" in zipfile.ZipFile(wheel).namelist()
