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


def test_seen_writes_new_info_at_once_and_last_seen_hourly(tmp_path: Path, clock: Clock) -> None:
    registry = open_registry(tmp_path, clock)
    bridge = registry.add("mac", "http://mac:8765", "t", INFO)
    # Seen every half hour: saved hourly, as the gateway may run from an SD card, but
    # still saved though no gap between calls is an hour long.
    for _ in range(4):
        clock.now += SEEN_WRITE_S / 2
        registry.seen(bridge.bridge_id, INFO)
    assert saved(tmp_path)[0]["last_seen"] == 1_000_000 + 2 * SEEN_WRITE_S
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


def test_never_follows_a_link_in_place_of_the_file(tmp_path: Path, clock: Clock) -> None:
    # The state folder's owner could plant one for a root-run CLI to read or write through.
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text('{"version": 1, "bridges": []}')
    (tmp_path / "bridges.json").symlink_to(elsewhere)
    with pytest.raises(RegistryError, match="Can't read"):
        open_registry(tmp_path, clock)
