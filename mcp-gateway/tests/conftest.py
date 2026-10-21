import os

import pytest


@pytest.fixture(autouse=True)
def no_settings_from_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings reads MCP_GATEWAY_* even when built directly; keep the caller's own out.
    for name in list(os.environ):
        if name.startswith("MCP_GATEWAY_"):
            monkeypatch.delenv(name)
