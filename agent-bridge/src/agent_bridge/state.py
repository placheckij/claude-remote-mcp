"""Where agent-bridge keeps its settings and state, and the state file itself.

Settings are an env file the platform's setup writes; state is what enrollment adds:
this bridge's id and the gateway it enrolled with. Both are readable only by the user.
"""

import contextlib
import os
import sys
import uuid
from pathlib import Path

from pydantic import BaseModel, ValidationError

APP = "agent-bridge"


class StateError(Exception):
    """A state file that can't be read or written; the message says which and why."""


class Gateway(BaseModel):
    url: str
    gateway_id: str
    # The host name the gateway knows this bridge by.
    name: str


class State(BaseModel):
    version: int = 1
    # Sent to the gateway at enrollment, and answered in /info so it can tell this bridge
    # is the one at the address given.
    bridge_id: str
    gateway: Gateway | None = None


def env_file() -> Path:
    if override := os.environ.get("AGENT_BRIDGE_ENV_FILE"):
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP / "env"
    if sys.platform == "win32":
        return _windows_folder("APPDATA", "Roaming") / APP / "env"
    return Path.home() / ".config" / APP / "env"


def state_file() -> Path:
    if override := os.environ.get("AGENT_BRIDGE_STATE_FILE"):
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP / "state.json"
    if sys.platform == "win32":
        return _windows_folder("LOCALAPPDATA", "Local") / APP / "state.json"
    return Path.home() / ".local" / "state" / APP / "state.json"


def _windows_folder(variable: str, below_app_data: str) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value else Path.home() / "AppData" / below_app_data


def load(path: Path) -> State | None:
    """The state, or None before there is any."""
    try:
        return State.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StateError(f"Can't read {path} ({error.strerror}).") from None
    except ValidationError:
        raise StateError(f"{path} isn't agent-bridge state; move it away to start over.") from None


def load_or_create(path: Path) -> State:
    """The state, made with a new bridge_id the first time."""
    state = load(path)
    if state is None:
        state = State(bridge_id=uuid.uuid4().hex)
        save(path, state)
    return state


def save(path: Path, state: State) -> None:
    """Written beside the file and renamed over it, so a crash never leaves half of it."""
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A leftover temporary file is removed first so its old mode can't stay.
        temporary.unlink(missing_ok=True)
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
            file.write(state.model_dump_json(indent=2))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except OSError as error:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise StateError(f"Can't write {path} ({error.strerror}).") from None
