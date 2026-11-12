import json
import os
import stat
from pathlib import Path

import pytest

from mcp_gateway.registry import SEEN_WRITE_S, Registry, RegistryError

INFO = {"api_version": 2, "name": "mac", "platform": "darwin"}


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def open_registry(state_dir: Path, clock: Clock) -> Registry:
    return Registry(state_dir / "bridges.json", clock)


def saved(state_dir: Path) -> list[dict]:
    return json.loads((state_dir / "bridges.json").read_text())["bridges"]


def test_starts_empty_without_a_file(tmp_path: Path, clock: Clock) -> None:
    assert open_registry(tmp_path, clock).bridges == []
    assert not (tmp_path / "bridges.json").exists()


def test_adds_a_bridge_to_memory_and_disk_together(tmp_path: Path, clock: Clock) -> None:
    registry = open_registry(tmp_path, clock)
    bridge = registry.add("mac", "http://192.168.1.20:8765", "t" * 32, INFO)
    assert registry.bridges == [bridge]
    assert saved(tmp_path) == [{
        "bridge_id": bridge.bridge_id, "name": "mac", "url": "http://192.168.1.20:8765", "token": "t" * 32,
        "info": INFO, "enrolled_at": 1_000_000, "last_seen": 1_000_000,
    }]
    # It holds tokens: only the gateway's user may read it.
    assert stat.S_IMODE((tmp_path / "bridges.json").stat().st_mode) == 0o600
    assert open_registry(tmp_path, clock).bridges == [bridge]


def test_a_bridge_never_seen_has_no_last_seen(tmp_path: Path, clock: Clock) -> None:
    bridge = open_registry(tmp_path, clock).add("mac", "http://mac:8765", "t", None)
    assert (bridge.info, bridge.last_seen) == (None, None)


@pytest.mark.parametrize("name", ["", "Mac", "1mac", "mac_book", "mac/1", "m" * 33, "-mac"])
def test_refuses_names_that_cant_qualify_an_agent_id(tmp_path: Path, clock: Clock, name: str) -> None:
    with pytest.raises(RegistryError, match="lowercase letter"):
        open_registry(tmp_path, clock).add(name, "http://mac:8765", "t", INFO)


def test_refuses_a_name_or_url_already_taken(tmp_path: Path, clock: Clock) -> None:
    registry = open_registry(tmp_path, clock)
    registry.add("mac", "http://mac:8765", "t", INFO)
    with pytest.raises(RegistryError, match="already a host named mac"):
        registry.add("mac", "http://other:8765", "t", INFO)
    with pytest.raises(RegistryError, match="already added as mac"):
        registry.add("linux", "http://mac:8765/", "t", INFO)


def test_renames_and_removes(tmp_path: Path, clock: Clock) -> None:
    registry = open_registry(tmp_path, clock)
    registry.add("mac", "http://mac:8765", "t", INFO)
    registry.add("linux", "http://linux:8765", "t", INFO)
    with pytest.raises(RegistryError, match="already a host named linux"):
        registry.rename("mac", "linux")
    with pytest.raises(RegistryError, match="No host named pi; hosts: mac, linux."):
        registry.rename("pi", "pc")
    registry.rename("mac", "studio")
    registry.remove("linux")
    assert [bridge.name for bridge in registry.bridges] == ["studio"]
    assert [bridge["name"] for bridge in saved(tmp_path)] == ["studio"]
    with pytest.raises(RegistryError, match="No host named linux"):
        registry.remove("linux")


def test_a_change_starts_from_the_file_as_it_is(tmp_path: Path, clock: Clock) -> None:
    # The CLI and the running gateway each hold a copy; neither may undo the other's change.
    gateway, cli = open_registry(tmp_path, clock), open_registry(tmp_path, clock)
    cli.add("mac", "http://mac:8765", "t", INFO)
    gateway.add("linux", "http://linux:8765", "t", INFO)
    assert [bridge["name"] for bridge in saved(tmp_path)] == ["mac", "linux"]


def test_reloads_only_when_the_file_changed(tmp_path: Path, clock: Clock) -> None:
    gateway, cli = open_registry(tmp_path, clock), open_registry(tmp_path, clock)
    assert not gateway.reload()
    cli.add("mac", "http://mac:8765", "t", INFO)
    assert gateway.reload()
    assert [bridge.name for bridge in gateway.bridges] == ["mac"]
    assert not gateway.reload()
    # Its own writes are not a change to reload.
    gateway.add("linux", "http://linux:8765", "t", INFO)
    assert not gateway.reload()
    (tmp_path / "bridges.json").unlink()
    assert gateway.reload() and gateway.bridges == []


def test_a_broken_file_stops_startup_but_not_a_running_gateway(tmp_path: Path, clock: Clock) -> None:
    registry = open_registry(tmp_path, clock)
    registry.add("mac", "http://mac:8765", "secret-token", INFO)
    (tmp_path / "bridges.json").write_text('{"version": 1, "bridges": [{"name": "mac", "token": "secret-token"}]}')
    with pytest.raises(RegistryError, match="bridges.json") as error:
        open_registry(tmp_path, clock)
    assert "secret-token" not in str(error.value)
    # Reloading keeps the bridges it had: a hand edit half done shouldn't cut every host off.
    assert not registry.reload()
    assert [bridge.name for bridge in registry.bridges] == ["mac"]


def test_seen_writes_new_info_at_once_and_last_seen_hourly(tmp_path: Path, clock: Clock) -> None:
    registry = open_registry(tmp_path, clock)
    bridge = registry.add("mac", "http://mac:8765", "t", INFO)
    clock.now += 60
    registry.seen(bridge.bridge_id, INFO)
    # In memory at once, on disk only hourly: the gateway runs on an SD card.
    assert registry.bridges[0].last_seen == 1_000_060
    assert saved(tmp_path)[0]["last_seen"] == 1_000_000
    clock.now += SEEN_WRITE_S
    registry.seen(bridge.bridge_id, INFO)
    assert saved(tmp_path)[0]["last_seen"] == 1_000_060 + SEEN_WRITE_S
    newer = {**INFO, "api_version": 3}
    registry.seen(bridge.bridge_id, newer)
    assert saved(tmp_path)[0]["info"] == newer


def test_seen_ignores_a_bridge_removed_meanwhile(tmp_path: Path, clock: Clock) -> None:
    gateway, cli = open_registry(tmp_path, clock), open_registry(tmp_path, clock)
    bridge = gateway.add("mac", "http://mac:8765", "t", None)
    cli.remove("mac")
    gateway.seen(bridge.bridge_id, INFO)
    assert saved(tmp_path) == [] and gateway.bridges == []


def test_seen_survives_a_disk_that_cant_be_written(
    tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    registry = open_registry(tmp_path, clock)
    bridge = registry.add("mac", "http://mac:8765", "t", None)
    os.chmod(tmp_path, 0o500)
    try:
        registry.seen(bridge.bridge_id, INFO)
    finally:
        os.chmod(tmp_path, 0o700)
    assert registry.bridges[0].info == INFO
    assert "Couldn't save" in caplog.text
