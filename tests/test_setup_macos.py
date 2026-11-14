"""agent-bridge/deploy/setup-macos.sh run against a temporary HOME, with uv, launchctl and curl
replaced by stubs: what it does with the token on a first install and on a re-run."""

import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="setup-macos.sh is for macOS")

SCRIPT = Path(__file__).parent.parent / "agent-bridge" / "deploy" / "setup-macos.sh"
LABEL = "dev.claude-remote.agent-bridge"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    # launchctl only logs, so a test never touches the real agent-bridge's launchd job.
    for name, body in {
        "uv": "exit 0",
        "launchctl": 'echo "$*" >> "$HOME/launchctl.log"',
        "curl": "echo 200",
        "herdr": "exit 0",
    }.items():
        stub = stubs / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    (tmp_path / "home" / "projects").mkdir(parents=True)
    return tmp_path / "home"


def setup(home: Path, **env: str) -> subprocess.CompletedProcess:
    stubs = home.parent / "stubs"
    result = subprocess.run(
        [str(SCRIPT)],
        env={
            "HOME": str(home),
            "PATH": f"{stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOST": "127.0.0.1",
            "PORT": "8765",
            "ALLOWED_ROOTS": str(home / "projects"),
            "HERDR_BIN": str(stubs / "herdr"),
            "AGENT_BRIDGE_CLAUDE_CONFIG_DIR": "",
            "TRUST_ROOT": "",
            **env,
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"bootstrap gui/{os.getuid()}" in (home / "launchctl.log").read_text()
    return result


def token(home: Path) -> str:
    plist = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["AGENT_BRIDGE_TOKEN"]


def test_a_first_install_generates_a_token(home: Path) -> None:
    result = setup(home)
    assert re.fullmatch(r"[0-9a-f]{64}", token(home))
    assert f"AGENT_BRIDGE_TOKEN={token(home)}   (new: give it to bridges add" in result.stdout


def test_a_rerun_keeps_the_installed_token(home: Path) -> None:
    setup(home)
    first = token(home)
    result = setup(home)
    assert token(home) == first
    assert "AGENT_BRIDGE_TOKEN is unchanged" in result.stdout
    assert first not in result.stdout


def test_rotate_token_replaces_the_installed_token(home: Path) -> None:
    setup(home)
    first = token(home)
    result = setup(home, ROTATE_TOKEN="1")
    assert token(home) != first
    assert "(new: give it to bridges add" in result.stdout


def test_a_token_passed_in_replaces_the_installed_one(home: Path) -> None:
    setup(home)
    given = "a" * 40
    result = setup(home, AGENT_BRIDGE_TOKEN=given)
    assert token(home) == given
    assert "AGENT_BRIDGE_TOKEN is the one you passed in" in result.stdout
