import json
import os
import stat
from pathlib import Path

import pytest

from mcp_gateway.registry import NameTaken, Registry, RegistryError, UnknownBridge

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


def written(path: Path) -> tuple[int, int]:
    # A write replaces the file, so its inode changes even within one tick of a coarse clock.
    status = path.stat()
    return status.st_ino, status.st_mtime_ns


def test_starts_empty_without_a_file(tmp_path: Path, clock: Clock) -> None:
    assert open_registry(tmp_path, clock).bridges == []
    assert not (tmp_path / "bridges.json").exists()


def test_adds_a_bridge_to_memory_and_disk_together(tmp_path: Path, clock: Clock) -> None:
    registry = open_registry(tmp_path, clock)
    bridge = registry.add("mac", "http://192.168.1.20:8765", "t" * 32, INFO)
    assert registry.bridges == [bridge]
    assert saved(tmp_path) == [{
        "bridge_id": bridge.bridge_id, "name": "mac", "url": "http://192.168.1.20:8765", "token": "t" * 32,
        "info": INFO, "enrolled_at": 1_000_000, "last_seen": None,
    }]
    # It holds tokens: only the gateway's user may read it.
    assert stat.S_IMODE((tmp_path / "bridges.json").stat().st_mode) == 0o600
    assert open_registry(tmp_path, clock).bridges == [bridge]


def test_a_bridge_that_never_answered_has_no_info(tmp_path: Path, clock: Clock) -> None:
    assert open_registry(tmp_path, clock).add("mac", "http://mac:8765", "t", None).info is None


def test_last_seen_is_kept_only_as_null(tmp_path: Path, clock: Clock) -> None:
    # An older gateway needs the key to read the file; what it saved there is dropped.
    record = {
        "bridge_id": "1" * 32, "name": "mac", "url": "http://mac:8765", "token": "t", "info": INFO,
        "enrolled_at": 1_000_000, "last_seen": 1_000_500,
    }
    (tmp_path / "bridges.json").write_text(json.dumps({"version": 1, "bridges": [record]}))
    registry = open_registry(tmp_path, clock)
    assert registry.bridges[0].last_seen is None
    registry.rename("mac", "studio")
    assert saved(tmp_path)[0]["last_seen"] is None


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


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a folder this user can't enter")
def test_a_file_it_cant_even_look_at_stops_startup_but_not_a_running_gateway(
    tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    state = tmp_path / "state"
    registry = Registry(state / "bridges.json", clock)
    registry.add("mac", "http://mac:8765", "t", INFO)
    os.chmod(state, 0o000)
    try:
        with pytest.raises(RegistryError, match="Can't read .*Permission denied"):
            Registry(state / "bridges.json", clock)
        # Kept, and logged once rather than on every request.
        assert not registry.reload() and not registry.reload()
    finally:
        os.chmod(state, 0o700)
    assert registry.names() == ["mac"]
    assert caplog.text.count("Keeping the agent hosts from before") == 1


@pytest.mark.parametrize(
    ("edit", "says"),
    [(lambda records: records[0].update(name="Mac"), "must be a host name"),
     (lambda records: records[1].update(name="mac"), "two hosts have the same name"),
     (lambda records: records[1].update(url="http://mac:8765"), "two hosts have the same url")],
)
def test_a_file_with_names_the_tools_cant_use_is_refused(tmp_path: Path, clock: Clock, edit, says: str) -> None:
    # A hand edit like these would leave a host unreachable, or let remove drop two.
    registry = open_registry(tmp_path, clock)
    registry.add("mac", "http://mac:8765", "t", INFO)
    registry.add("linux", "http://linux:8765", "t", INFO)
    records = saved(tmp_path)
    edit(records)
    (tmp_path / "bridges.json").write_text(json.dumps({"version": 1, "bridges": records}))
    with pytest.raises(RegistryError, match=says):
        open_registry(tmp_path, clock)
    assert not registry.reload() and registry.names() == ["mac", "linux"]


@pytest.mark.parametrize("info", [{"platform": "darwin"}, {"api_version": "2"}, {"api_version": 2.5}])
def test_a_record_whose_info_has_no_api_version_is_refused(tmp_path: Path, clock: Clock, info: dict) -> None:
    # The gateway compares api_version as a whole number to decide whether to call the host.
    registry = open_registry(tmp_path, clock)
    registry.add("mac", "http://mac:8765", "t", INFO)
    records = saved(tmp_path)
    records[0]["info"] = info
    (tmp_path / "bridges.json").write_text(json.dumps({"version": 1, "bridges": records}))
    assert not registry.reload() and registry.bridges[0].info == INFO


@pytest.mark.parametrize(("field", "value"), [("url", "http://192.168.1.20:87a5"), ("token", "abcą")])
def test_a_record_no_client_can_be_made_for_is_refused(
    tmp_path: Path, clock: Clock, field: str, value: str
) -> None:
    # A hand edit like this would otherwise fail every host at once, not just its own.
    registry = open_registry(tmp_path, clock)
    registry.add("mac", "http://mac:8765", "t", INFO)
    records = saved(tmp_path)
    records[0][field] = value
    (tmp_path / "bridges.json").write_text(json.dumps({"version": 1, "bridges": records}))
    assert not registry.reload() and registry.bridges[0].url == "http://mac:8765"
    with pytest.raises(RegistryError, match=f"The {field} must be") as error:
        registry.add("linux", value if field == "url" else "http://linux:8765", value if field == "token" else "t", INFO)
    assert value not in str(error.value)


def test_update_info_writes_only_a_changed_info(tmp_path: Path, clock: Clock) -> None:
    # Most calls ask /info; the gateway may run from an SD card, so the same answer isn't saved again.
    registry = open_registry(tmp_path, clock)
    bridge = registry.add("mac", "http://mac:8765", "t", INFO)
    before = (tmp_path / "bridges.json").stat().st_mtime_ns
    clock.now += 86_400
    registry.update_info(bridge.bridge_id, dict(INFO))
    assert (tmp_path / "bridges.json").stat().st_mtime_ns == before
    newer = {**INFO, "api_version": 3}
    registry.update_info(bridge.bridge_id, newer)
    assert saved(tmp_path)[0]["info"] == newer


def test_move_writes_only_a_new_address(tmp_path: Path, clock: Clock) -> None:
    # A bridge reports its address on every start, mostly the one it had.
    registry = open_registry(tmp_path, clock)
    bridge = registry.add("mac", "http://mac:8765", "t", INFO)
    before = written(tmp_path / "bridges.json")
    assert registry.move(bridge.bridge_id, "http://mac:8765/") == bridge
    assert written(tmp_path / "bridges.json") == before
    registry.move(bridge.bridge_id, "http://mac:9999")
    assert saved(tmp_path)[0]["url"] == "http://mac:9999"


def test_move_refuses_a_bridge_removed_meanwhile(tmp_path: Path, clock: Clock) -> None:
    # E.g. `bridges remove` while the gateway asks the bridge's /info: it must enroll again.
    gateway, cli = open_registry(tmp_path, clock), open_registry(tmp_path, clock)
    bridge = gateway.add("mac", "http://mac:8765", "t", INFO)
    cli.remove("mac")
    with pytest.raises(UnknownBridge):
        gateway.move(bridge.bridge_id, "http://mac:8765")
    assert saved(tmp_path) == [] and gateway.bridges == []


def test_update_info_ignores_a_bridge_removed_meanwhile(tmp_path: Path, clock: Clock) -> None:
    gateway, cli = open_registry(tmp_path, clock), open_registry(tmp_path, clock)
    bridge = gateway.add("mac", "http://mac:8765", "t", None)
    cli.remove("mac")
    gateway.update_info(bridge.bridge_id, INFO)
    assert saved(tmp_path) == [] and gateway.bridges == []


def test_update_info_survives_a_disk_that_cant_be_written(
    tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    registry = open_registry(tmp_path, clock)
    bridge = registry.add("mac", "http://mac:8765", "t", None)
    os.chmod(tmp_path, 0o500)
    try:
        registry.update_info(bridge.bridge_id, INFO)
    finally:
        os.chmod(tmp_path, 0o700)
    assert registry.bridges[0].info == INFO
    assert "Couldn't save" in caplog.text
    # Saved with the next answer, though it is the same as the one kept in memory.
    registry.update_info(bridge.bridge_id, INFO)
    assert saved(tmp_path)[0]["info"] == INFO


def test_info_kept_in_memory_outlasts_other_changes(tmp_path: Path, clock: Clock) -> None:
    gateway, cli = open_registry(tmp_path, clock), open_registry(tmp_path, clock)
    bridge = gateway.add("mac", "http://mac:8765", "t", INFO)
    newer = {**INFO, "api_version": 3}
    os.chmod(tmp_path, 0o500)
    try:
        gateway.update_info(bridge.bridge_id, newer)
        # Neither a change that changes nothing nor a refused one drops it.
        gateway.move(bridge.bridge_id, "http://mac:8765")
        with pytest.raises(UnknownBridge):
            gateway.move("9" * 32, "http://mac:8765")
    finally:
        os.chmod(tmp_path, 0o700)
    assert gateway.bridges[0].info == newer
    # Nor another process's change, nor one of its own.
    cli.rename("mac", "studio")
    assert gateway.reload() and gateway.bridges[0].info == newer
    gateway.rename("studio", "mac")
    assert gateway.bridges[0].info == newer and saved(tmp_path)[0]["info"] == INFO
    gateway.update_info(bridge.bridge_id, newer)
    assert saved(tmp_path)[0]["info"] == newer


def test_re_enrolling_replaces_info_kept_in_memory(tmp_path: Path, clock: Clock) -> None:
    registry = open_registry(tmp_path, clock)
    bridge = registry.enroll("1" * 32, "mac", "http://mac:8765", "t", INFO)
    registry.add("linux", "http://linux:8765", "t", INFO)
    newer = {**INFO, "api_version": 3}
    os.chmod(tmp_path, 0o500)
    try:
        registry.update_info(bridge.bridge_id, newer)
    finally:
        os.chmod(tmp_path, 0o700)
    # One refused keeps it.
    with pytest.raises(NameTaken):
        registry.enroll("1" * 32, "linux", "http://mac:8765", "t", {**INFO, "platform": "linux"})
    assert registry.bridges[0].info == newer
    fresh = {**INFO, "platform": "linux"}
    registry.enroll("1" * 32, "mac", "http://mac:8765", "t", fresh)
    assert registry.bridges[0].info == fresh
    registry.rename("mac", "studio")
    assert registry.bridges[0].info == fresh


def test_never_follows_a_link_in_place_of_the_file(tmp_path: Path, clock: Clock) -> None:
    # The state folder's owner could plant one for a root-run CLI to read or write through.
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text('{"version": 1, "bridges": []}')
    (tmp_path / "bridges.json").symlink_to(elsewhere)
    with pytest.raises(RegistryError, match="Can't read"):
        open_registry(tmp_path, clock)
