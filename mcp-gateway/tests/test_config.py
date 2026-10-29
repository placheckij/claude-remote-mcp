from pathlib import Path

import pytest

from mcp_gateway.config import Settings

REQUIRED = {
    "MCP_GATEWAY_OAUTH_PASSPHRASE": "p" * 24,
    "MCP_GATEWAY_STATE_DIR": "/var/lib/mcp-gateway",
    "MCP_GATEWAY_BRIDGE_URL": "http://agents.lan:8765",
    "MCP_GATEWAY_BRIDGE_TOKEN": "b" * 32,
    "MCP_GATEWAY_PUBLIC_HOST": "mcp.example.com",
    "MCP_GATEWAY_PROJECTS_ROOT": "/home/me/projects",
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    # from_env reads .env from the working directory; start each test without one.
    monkeypatch.chdir(tmp_path)
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_reads_every_setting(env: pytest.MonkeyPatch) -> None:
    settings = Settings.from_env()
    assert (settings.bridge_url, settings.projects_root) == ("http://agents.lan:8765", "/home/me/projects")
    assert settings.state_dir == Path("/var/lib/mcp-gateway")


def test_the_static_token_is_optional(env: pytest.MonkeyPatch) -> None:
    # Unset, only OAuth tokens are accepted.
    assert Settings.from_env().token is None
    env.setenv("MCP_GATEWAY_TOKEN", "g" * 32)
    assert Settings.from_env().token == "g" * 32


@pytest.mark.parametrize("name", sorted(REQUIRED))
def test_every_setting_is_required(env: pytest.MonkeyPatch, name: str) -> None:
    env.delenv(name)
    with pytest.raises(RuntimeError, match=f"{name}: must be set"):
        Settings.from_env()


def test_an_empty_setting_counts_as_unset(env: pytest.MonkeyPatch) -> None:
    env.setenv("MCP_GATEWAY_PUBLIC_HOST", "")
    with pytest.raises(RuntimeError, match="MCP_GATEWAY_PUBLIC_HOST: must be set"):
        Settings.from_env()


def test_refuses_a_short_token(env: pytest.MonkeyPatch) -> None:
    env.setenv("MCP_GATEWAY_TOKEN", "short")
    with pytest.raises(RuntimeError, match="MCP_GATEWAY_TOKEN: .*at least 32"):
        Settings.from_env()


def test_refuses_a_short_passphrase(env: pytest.MonkeyPatch) -> None:
    env.setenv("MCP_GATEWAY_OAUTH_PASSPHRASE", "hunter2")
    with pytest.raises(RuntimeError, match="MCP_GATEWAY_OAUTH_PASSPHRASE: .*at least 16"):
        Settings.from_env()


def test_reads_a_dotenv_file_under_the_environment(env: pytest.MonkeyPatch) -> None:
    env.delenv("MCP_GATEWAY_PROJECTS_ROOT")
    Path(".env").write_text(
        "MCP_GATEWAY_PROJECTS_ROOT=/srv/projects\nMCP_GATEWAY_PUBLIC_HOST=dotenv.example.com\n"
        "AGENT_BRIDGE_TOKEN=not-ours\n"
    )
    settings = Settings.from_env()
    assert (settings.projects_root, settings.public_host) == ("/srv/projects", "mcp.example.com")
