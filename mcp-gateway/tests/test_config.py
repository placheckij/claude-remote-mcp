import pytest

from mcp_gateway.config import Settings

REQUIRED = {
    "MCP_GATEWAY_TOKEN": "g" * 32,
    "MCP_GATEWAY_BRIDGE_URL": "http://agents.lan:8765",
    "MCP_GATEWAY_BRIDGE_TOKEN": "b" * 32,
    "MCP_GATEWAY_PUBLIC_HOST": "mcp.example.com",
    "MCP_GATEWAY_PROJECTS_ROOT": "/home/me/projects",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_reads_every_setting(env: pytest.MonkeyPatch) -> None:
    settings = Settings.from_env()
    assert (settings.bridge_url, settings.projects_root) == ("http://agents.lan:8765", "/home/me/projects")


@pytest.mark.parametrize("name", sorted(REQUIRED))
def test_every_setting_is_required(env: pytest.MonkeyPatch, name: str) -> None:
    env.delenv(name)
    with pytest.raises(RuntimeError, match=name):
        Settings.from_env()
