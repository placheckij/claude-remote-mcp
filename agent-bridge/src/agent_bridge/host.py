"""What /info says about the agent host itself."""

import re
import socket
import sys
from importlib.metadata import version

# 1 is the API before /info.
API_VERSION = 2


def host_name() -> str:
    """The hostname as a slug, e.g. "Jakubs-MacBook-Pro.local" → "jakubs-macbook-pro"."""
    first = socket.gethostname().split(".")[0].lower()
    return re.sub(r"[^a-z0-9]+", "-", first).strip("-") or "agent-host"


def platform_name() -> str:
    """"darwin", "linux" or "windows" (sys.platform says "win32" on 64-bit Windows too)."""
    return "windows" if sys.platform.startswith("win") else sys.platform


def bridge_version() -> str:
    return version("agent-bridge")
