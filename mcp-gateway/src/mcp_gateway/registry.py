"""The agent hosts the gateway knows: bridges.json in the state folder, one record per
agent-bridge, each with its own token.

`mcp-gateway bridges` (cli.py) changes the file while the gateway runs. So every change
takes bridges.lock and starts from the file as it is on disk, and the gateway reloads the
file when it sees it changed (reload()).
"""

import contextlib
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

try:
    import fcntl
except ImportError:  # Windows: no lock, as for oauth.lock.
    fcntl = None

log = logging.getLogger(__name__)

# Nothing here follows a link put in place of the lock or the file.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

# The host part of an agent id ("mac/w1:p1"): short, speakable and without "/" or ":".
NAME_PATTERN = r"[a-z][a-z0-9-]{0,31}"
# Every call refreshes last_seen in memory; on disk only this often, as the gateway's host
# may run from an SD card. A changed /info is written at once.
SEEN_WRITE_S = 3600


class RegistryError(Exception):
    """A change the registry refuses, or a file it can't read; the message says why."""


class Bridge(BaseModel):
    model_config = ConfigDict(frozen=True)

    bridge_id: str
    name: str
    url: str
    token: str
    # The last /info: {"api_version": 1} for an agent-bridge from before /info, None until
    # it first answers.
    info: dict | None
    enrolled_at: int
    last_seen: int | None


class _File(BaseModel):
    version: int = 1
    bridges: list[Bridge]


class Registry:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self._lock_path = path.with_name("bridges.lock")
        self.bridges, self._stamp = self._read()
        # Counts every time `bridges` is taken from the file, which may bring in another
        # process's changes: by a reload, or by a change of this process's own.
        self.generation = 0

    def names(self) -> list[str]:
        return [bridge.name for bridge in self.bridges]

    def reload(self) -> bool:
        """Takes in a change another process made; True if there was one. A file that can't
        be read keeps the bridges from before, with an error in the log."""
        if self._file_stamp() == self._stamp:
            return False
        try:
            self.bridges, self._stamp = self._read()
        except RegistryError as error:
            # Logged once per change to the file, not on every call until it's fixed.
            self._stamp = self._file_stamp()
            log.error("%s Keeping the agent hosts from before.", error)
            return False
        self.generation += 1
        return True

    def add(self, name: str, url: str, token: str, info: dict | None) -> Bridge:
        url = url.rstrip("/")
        _check_name(name)
        now = int(self.clock())
        bridge = Bridge(
            bridge_id=uuid.uuid4().hex, name=name, url=url, token=token, info=info,
            enrolled_at=now, last_seen=now if info is not None else None,
        )

        def add(bridges: list[Bridge]) -> list[Bridge]:
            _check_free(bridges, name)
            for other in bridges:
                if other.url == url:
                    raise RegistryError(f"{url} is already added as {other.name}.")
            return [*bridges, bridge]

        self._change(add)
        return bridge

    def rename(self, name: str, new_name: str) -> None:
        _check_name(new_name)

        def rename(bridges: list[Bridge]) -> list[Bridge]:
            _find(bridges, name)
            if new_name != name:
                _check_free(bridges, new_name)
            return [b.model_copy(update={"name": new_name}) if b.name == name else b for b in bridges]

        self._change(rename)

    def remove(self, name: str) -> None:
        def remove(bridges: list[Bridge]) -> list[Bridge]:
            _find(bridges, name)
            return [bridge for bridge in bridges if bridge.name != name]

        self._change(remove)

    def seen(self, bridge_id: str, info: dict) -> None:
        """Notes that a bridge answered /info. Kept in memory if it can't be saved: the
        gateway works on without it."""
        now = int(self.clock())
        current = next((bridge for bridge in self.bridges if bridge.bridge_id == bridge_id), None)
        if current is None:
            return
        update = {"info": info, "last_seen": now}
        # last_seen in memory is the one on disk: only a save moves it.
        stale = current.last_seen is None or now - current.last_seen >= SEEN_WRITE_S
        if current.info == info and not stale:
            return
        try:
            self._change(
                lambda bridges: [b.model_copy(update=update) if b.bridge_id == bridge_id else b for b in bridges]
            )
        except (OSError, RegistryError) as error:
            log.warning("Couldn't save what %s said about itself in %s (%s).", current.name, self.path, error)
            self.bridges = [b.model_copy(update=update) if b.bridge_id == bridge_id else b for b in self.bridges]

    # --- The file ----------------------------------------------------------------------

    def _change(self, change: Callable[[list[Bridge]], list[Bridge]]) -> None:
        """Applies `change` to the file as it is now, under the lock, then to memory."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            bridges, _ = self._read()
            bridges = change(bridges)
            self._write(bridges)
            self.bridges, self._stamp = bridges, self._file_stamp()
            self.generation += 1
        finally:
            os.close(lock)

    def _read(self) -> tuple[list[Bridge], tuple | None]:
        stamp = self._file_stamp()
        if stamp is None:
            return [], None
        try:
            with os.fdopen(os.open(self.path, os.O_RDONLY | _NOFOLLOW), "rb") as file:
                text = file.read()
        except OSError as error:
            raise RegistryError(f"Can't read {self.path} ({error.strerror}).") from None
        try:
            state = _File.model_validate_json(text)
        except ValidationError as error:
            # Without the values: a record holds a token.
            problem = "; ".join(
                f"{'.'.join(str(part) for part in item['loc']) or 'file'}: {item['msg']}"
                for item in error.errors(include_input=False, include_url=False)
            )
            raise RegistryError(f"Can't read the agent hosts in {self.path} ({problem}).") from None
        return state.bridges, stamp

    def _write(self, bridges: list[Bridge]) -> None:
        data = _File(bridges=bridges).model_dump(mode="json")
        # As oauth.json: written beside it and renamed over it, so a crash never leaves half
        # a file, and a leftover temporary file is removed first so its old mode can't stay.
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        temporary.unlink(missing_ok=True)
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
                json.dump(data, file, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise

    def _file_stamp(self) -> tuple | None:
        try:
            status = self.path.stat()
        except FileNotFoundError:
            return None
        return status.st_mtime_ns, status.st_size, status.st_ino


def _check_name(name: str) -> None:
    if not re.fullmatch(NAME_PATTERN, name):
        raise RegistryError(
            f"{name!r} can't name a host: use a lowercase letter, then up to 31 lowercase "
            'letters, digits or "-", like "mac" or "linux-box".'
        )


def _check_free(bridges: list[Bridge], name: str) -> None:
    if any(bridge.name == name for bridge in bridges):
        raise RegistryError(f"There is already a host named {name}.")


def _find(bridges: list[Bridge], name: str) -> Bridge:
    for bridge in bridges:
        if bridge.name == name:
            return bridge
    hosts = ", ".join(bridge.name for bridge in bridges) or "none"
    raise RegistryError(f"No host named {name}; hosts: {hosts}.")
