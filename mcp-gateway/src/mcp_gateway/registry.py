"""The agent hosts the gateway knows: bridges.json in the state folder, one record per
agent-bridge, each with its own token.

`mcp-gateway bridges` (cli.py) changes the file while the gateway runs. So every change
takes bridges.lock and starts from the file as it is on disk, and the gateway reloads the
file when it sees it changed (reload()). Enrolled bridges (enrollment.py) change their own
records by their bridge_id.
"""

import logging
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import httpx
from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError, model_validator

from mcp_gateway.files import locked, read_bytes, write_json

log = logging.getLogger(__name__)

# The host part of an agent id ("mac/w1:p1"): short, speakable and without "/" or ":".
NAME_PATTERN = r"[a-z][a-z0-9-]{0,31}"


class RegistryError(Exception):
    """A change the registry refuses, or a file it can't read; the message says why."""


class NameTaken(RegistryError):
    pass


class UnknownBridge(RegistryError):
    pass


def check_url(value: str) -> str:
    """An agent-bridge's address a client can be made for; also checks the legacy setting."""
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        url = None
    if url is None or url.scheme not in ("http", "https") or not url.host:
        raise ValueError("must be the agent-bridge's address, like http://192.168.1.20:8765")
    return value


def check_token(value: str) -> str:
    if not (value.isascii() and value.isprintable()):
        raise ValueError("must be printable ASCII, as it goes in an HTTP header")
    return value


def check_info(value: dict | None) -> dict | None:
    # The gateway decides by api_version whether to call the host, as an int.
    if value is not None and type(value.get("api_version")) is not int:
        raise ValueError("must have a whole-number api_version")
    return value


def check_name_format(value: str) -> str:
    if not re.fullmatch(NAME_PATTERN, value):
        raise ValueError("must be a host name, like mac or linux-box")
    return value


class Bridge(BaseModel):
    model_config = ConfigDict(frozen=True)

    bridge_id: str
    # Checked on reading too, as the tools only take names of this form.
    name: Annotated[str, AfterValidator(check_name_format)]
    # Checked on reading too: a hand edit that no client can be made for would otherwise
    # fail every host, not just its own.
    url: Annotated[str, AfterValidator(check_url)]
    token: Annotated[str, AfterValidator(check_token)]
    # The last /info: {"api_version": 1} for an agent-bridge from before /info, None until
    # it first answers.
    info: Annotated[dict | None, AfterValidator(check_info)]
    enrolled_at: int
    # Always null: kept so an older gateway can still read the file.
    last_seen: Annotated[int | None, AfterValidator(lambda _: None)] = None


class _File(BaseModel):
    version: int = 1
    bridges: list[Bridge]

    @model_validator(mode="after")
    def _one_record_each(self) -> "_File":
        # As add() keeps them; a hand edit with two "mac"s would make remove drop both.
        for field in ("bridge_id", "name", "url"):
            values = [getattr(bridge, field) for bridge in self.bridges]
            if len(values) != len(set(values)):
                raise ValueError(f"two hosts have the same {field}")
        return self


class Registry:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self._lock_path = path.with_name("bridges.lock")
        self.bridges, self._stamp = self._read()
        # The last problem reload() logged, so it isn't logged on every call.
        self._problem: str | None = None
        # Bridges whose /info update_info kept in memory only, to save with the next answer.
        self._unsaved: set[str] = set()
        # Counts every time `bridges` is taken from the file, which may bring in another
        # process's changes: by a reload, or by a change of this process's own.
        self.generation = 0

    def names(self) -> list[str]:
        return [bridge.name for bridge in self.bridges]

    def reload(self) -> bool:
        """Takes in a change another process made; True if there was one. A file that can't
        be read keeps the bridges from before, with an error in the log."""
        try:
            stamp = self._file_stamp()
            if stamp == self._stamp:
                return False
            # Not read again until it changes, however it went.
            self._stamp = stamp
            self.bridges, self._stamp = self._read()
        except RegistryError as error:
            # Logged once per problem, not on every call until it's fixed.
            if str(error) != self._problem:
                log.error("%s Keeping the agent hosts from before.", error)
            self._problem = str(error)
            return False
        self._problem = None
        self.generation += 1
        return True

    def add(self, name: str, url: str, token: str, info: dict | None) -> Bridge:
        bridge = self._new(uuid.uuid4().hex, name, url, token, info)

        def add(bridges: list[Bridge]) -> list[Bridge]:
            _check_free(bridges, name)
            for other in bridges:
                if other.url == bridge.url:
                    raise RegistryError(f"{bridge.url} is already added as {other.name}.")
            return [*bridges, bridge]

        self._change(add)
        return bridge

    def enroll(self, bridge_id: str, name: str, url: str, token: str, info: dict) -> Bridge:
        """Adds a bridge that enrolled itself, or replaces its record if it enrolled before.
        Its /info has just answered at `url` as this bridge, so another record there is
        one left from before, e.g. a reinstall or a `bridges add`, and is dropped."""
        bridge = self._new(bridge_id, name, url, token, info)

        def enroll(bridges: list[Bridge]) -> list[Bridge]:
            stale = [b for b in bridges if b.bridge_id != bridge_id and b.url == bridge.url]
            others = [b for b in bridges if b.bridge_id != bridge_id and b not in stale]
            if any(other.name == name for other in others):
                raise NameTaken(f"There is already a host named {name}.")
            for record in stale:
                log.warning("Dropping %s: %s now answers as the agent host enrolling.", record.name, record.url)
            # In its old place if it had one, so `bridges list` keeps its order.
            kept = [bridge if b.bridge_id == bridge_id else b for b in bridges if b not in stale]
            return kept if bridge in kept else [*kept, bridge]

        self._change(enroll)
        return bridge

    def move(self, bridge_id: str, url: str) -> Bridge:
        """Gives an enrolled bridge a new address. Its /info has just answered there as this
        bridge, so another record there is out of date, e.g. after two hosts swapped
        addresses, and is dropped, as enroll() does. The address it had isn't saved again,
        as a bridge reports it on every start and the gateway's host may run from an SD card."""
        url = url.rstrip("/")

        def move(bridges: list[Bridge]) -> list[Bridge]:
            current = _find_id(bridges, bridge_id)
            stale = [b for b in bridges if b.bridge_id != bridge_id and b.url == url]
            for record in stale:
                log.warning("Dropping %s: %s now answers as %s.", record.name, record.url, current.name)
            moved = current.model_copy(update={"url": url})
            return [moved if b.bridge_id == bridge_id else b for b in bridges if b not in stale]

        self._change(move)
        return _find_id(self.bridges, bridge_id)

    def forget(self, bridge_id: str) -> Bridge:
        """Removes a bridge by its id, as it does itself when uninstalled."""
        found: list[Bridge] = []

        def forget(bridges: list[Bridge]) -> list[Bridge]:
            found.append(_find_id(bridges, bridge_id))
            return [b for b in bridges if b.bridge_id != bridge_id]

        self._change(forget)
        return found[0]

    def find_id(self, bridge_id: str) -> Bridge | None:
        return next((b for b in self.bridges if b.bridge_id == bridge_id), None)

    def _new(self, bridge_id: str, name: str, url: str, token: str, info: dict | None) -> Bridge:
        _check_name(name)
        now = int(self.clock())
        try:
            return Bridge(
                bridge_id=bridge_id, name=name, url=url.rstrip("/"), token=token, info=info,
                enrolled_at=now,
            )
        except ValidationError as error:
            # Without the input: it may be the token.
            field, problem = error.errors()[0]["loc"][0], error.errors()[0]["msg"].removeprefix("Value error, ")
            raise RegistryError(f"The {field} {problem}.") from None

    def rename(self, name: str, new_name: str) -> None:
        _check_name(new_name)

        def rename(bridges: list[Bridge]) -> list[Bridge]:
            _find(bridges, name)
            if new_name != name:
                _check_free(bridges, new_name)
            return [b.model_copy(update={"name": new_name}) if b.name == name else b for b in bridges]

        self._change(rename)

    def rename_id(self, bridge_id: str, new_name: str) -> Bridge:
        """Renames a bridge by its id, as it does itself (`agent-bridge rename`)."""
        _check_name(new_name)

        def rename(bridges: list[Bridge]) -> list[Bridge]:
            _find_id(bridges, bridge_id)
            if any(b.name == new_name and b.bridge_id != bridge_id for b in bridges):
                raise NameTaken(f"There is already a host named {new_name}.")
            return [b.model_copy(update={"name": new_name}) if b.bridge_id == bridge_id else b for b in bridges]

        self._change(rename)
        return _find_id(self.bridges, bridge_id)

    def remove(self, name: str) -> None:
        def remove(bridges: list[Bridge]) -> list[Bridge]:
            _find(bridges, name)
            return [bridge for bridge in bridges if bridge.name != name]

        self._change(remove)

    def update_info(self, bridge_id: str, info: dict) -> None:
        """Notes what a bridge answered /info. Saved only when it changed, i.e. after the
        bridge was updated or set up anew, as the gateway's host may run from an SD card.
        Kept in memory if it can't be saved: the gateway works on without it."""
        current = self.find_id(bridge_id)
        if current is None or (current.info == info and bridge_id not in self._unsaved):
            return
        update = {"info": info}
        try:
            self._change(
                lambda bridges: [b.model_copy(update=update) if b.bridge_id == bridge_id else b for b in bridges]
            )
        except (OSError, RegistryError) as error:
            log.warning("Couldn't save what %s said about itself in %s (%s).", current.name, self.path, error)
            self.bridges = [b.model_copy(update=update) if b.bridge_id == bridge_id else b for b in self.bridges]
            self._unsaved.add(bridge_id)
            return
        self._unsaved.discard(bridge_id)

    # --- The file ----------------------------------------------------------------------

    def _change(self, change: Callable[[list[Bridge]], list[Bridge]]) -> None:
        """Applies `change` to the file as it is now, under the lock, then to memory. Memory
        takes in the file even if `change` refuses it, and a change that leaves the records
        as they were isn't written."""
        with locked(self._lock_path):
            self.bridges, self._stamp = self._read()
            self.generation += 1
            bridges = change(self.bridges)
            if bridges != self.bridges:
                write_json(self.path, _File(bridges=bridges).model_dump(mode="json"))
                self.bridges, self._stamp = bridges, self._file_stamp()

    def _read(self) -> tuple[list[Bridge], tuple | None]:
        stamp = self._file_stamp()
        if stamp is None:
            return [], None
        try:
            text = read_bytes(self.path)
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

    def _file_stamp(self) -> tuple | None:
        try:
            status = self.path.stat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise RegistryError(f"Can't read {self.path} ({error.strerror}).") from None
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


def _find_id(bridges: list[Bridge], bridge_id: str) -> Bridge:
    for bridge in bridges:
        if bridge.bridge_id == bridge_id:
            return bridge
    raise UnknownBridge("This agent host isn't enrolled.")


def _find(bridges: list[Bridge], name: str) -> Bridge:
    for bridge in bridges:
        if bridge.name == name:
            return bridge
    hosts = ", ".join(bridge.name for bridge in bridges) or "none"
    raise RegistryError(f"No host named {name}; hosts: {hosts}.")
