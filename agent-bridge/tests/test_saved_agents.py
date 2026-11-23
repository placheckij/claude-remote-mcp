import os
import sys
import time
from pathlib import Path

import psutil
import pytest
from fakes import FakeRunner, err, ok

from agent_bridge import saved_agents
from agent_bridge.herdr import Herdr, HerdrError
from agent_bridge.saved_agents import SavedAgent, SavedAgents
from agent_bridge.state import StateError

UUID = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-2222-3333-4444-555555555555"


def entry(terminal_id: str = "t1", misses: int = 0, **fields) -> SavedAgent:
    values = {
        "name": "webapp", "cwd": "/work/webapp", "config_dir": None, "args": [], "session_id": None,
        "pane_id": "w1:p1", "terminal_id": terminal_id, "misses": misses, **fields,
    }
    return SavedAgent(**values)


def herdr_with(agents: list[dict], argv: list[str] | None = None, pid: int = os.getpid()) -> Herdr:
    process = {"pid": pid, "name": "claude", "argv0": "claude", "argv": argv or ["claude"], "cwd": "/x"}
    node = {"pid": pid + 1, "name": "node", "argv0": "node", "argv": ["node", "mcp.js"], "cwd": "/x"}
    info = {"foreground_process_group_id": pid, "foreground_processes": [node, process]}
    return Herdr(runner=FakeRunner({
        ("agent", "list"): ok({"agents": agents}),
        ("pane", "process-info"): ok({"process_info": info}),
    }))


def claude_agent(session: dict | None = None) -> dict:
    agent = {
        "agent": "claude", "name": "webapp", "pane_id": "w1:p1", "terminal_id": "t1",
        "cwd": "/work/webapp", "foreground_cwd": "/work/webapp/sub", "workspace_id": "w1",
    }
    if session is not None:
        agent["agent_session"] = session
    return agent


@pytest.mark.asyncio
async def test_snapshot_reads_name_cwd_account_args_and_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psutil.Process, "environ", lambda self: {"CLAUDE_CONFIG_DIR": "/cfg"})
    argv = ["claude", "--permission-mode", "auto", "--remote-control", "webapp", "--session-id", UUID]
    herdr = herdr_with([claude_agent()], argv)

    [saved] = await saved_agents.snapshot(herdr)

    assert saved.args == ["--permission-mode", "auto", "--remote-control", "webapp"]
    assert saved.session_id == UUID
    assert saved.config_dir == "/cfg"
    assert not saved.config_dir_unknown
    assert (saved.name, saved.cwd, saved.pane_id, saved.terminal_id) == ("webapp", "/work/webapp", "w1:p1", "t1")

    session = {"agent": "claude", "kind": "id", "source": "herdr:claude", "value": OTHER}
    [saved] = await saved_agents.snapshot(herdr_with([claude_agent(session)], argv))
    assert saved.session_id == OTHER


@pytest.mark.asyncio
async def test_snapshot_skips_non_claude_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psutil.Process, "environ", lambda self: {})
    other = {**claude_agent(), "agent": "codex"}
    assert await saved_agents.snapshot(herdr_with([other])) == []


@pytest.mark.asyncio
async def test_snapshot_unreadable_environ_marks_config_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(self):
        raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil.Process, "environ", denied)
    [saved] = await saved_agents.snapshot(herdr_with([claude_agent()], ["claude", "--model", "opus"]))
    assert saved.config_dir is None
    assert saved.config_dir_unknown
    assert saved.args == ["--model", "opus"]


@pytest.mark.asyncio
async def test_snapshot_skips_a_pane_whose_process_info_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psutil.Process, "environ", lambda self: {})
    second = {**claude_agent(), "pane_id": "w2:p1", "terminal_id": "t2"}
    infos = [err("pane_not_found"), ok({"process_info": {
        "foreground_process_group_id": 7,
        "foreground_processes": [{"pid": 7, "name": "claude", "argv0": "claude", "argv": ["claude"]}],
    }})]
    herdr = Herdr(runner=FakeRunner({
        ("agent", "list"): ok({"agents": [claude_agent(), second]}),
        ("pane", "process-info"): infos,
    }))
    assert [saved.terminal_id for saved in await saved_agents.snapshot(herdr)] == ["t2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["cwd", "terminal_id"])
async def test_snapshot_skips_an_agent_without_cwd_or_terminal(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    monkeypatch.setattr(psutil.Process, "environ", lambda self: {})
    lacking = {key: value for key, value in claude_agent().items() if key != missing}
    nulled = {**claude_agent(), "pane_id": "w2:p1", missing: None}
    second = {**claude_agent(), "pane_id": "w3:p1", "terminal_id": "t3"}
    snapshot = await saved_agents.snapshot(herdr_with([lacking, nulled, second]))
    assert [saved.terminal_id for saved in snapshot] == ["t3"]


@pytest.mark.asyncio
async def test_snapshot_lets_a_failing_agent_list_propagate() -> None:
    herdr = Herdr(runner=FakeRunner({("agent", "list"): err("server_down")}))
    with pytest.raises(HerdrError):
        await saved_agents.snapshot(herdr)


def test_merge_new_entry_and_seen_resets_misses() -> None:
    merged = saved_agents.merge([entry("t1", misses=1)], [entry("t1", name="renamed"), entry("t2")], False)
    assert {saved.terminal_id: (saved.name, saved.misses) for saved in merged} == {
        "t1": ("renamed", 0), "t2": ("webapp", 0),
    }


def test_one_miss_keeps_entry() -> None:
    [kept] = saved_agents.merge([entry("t1")], [], False)
    assert kept.misses == 1


def test_two_misses_drop_entry() -> None:
    assert saved_agents.merge([entry("t1", misses=1)], [], False) == []


def test_restart_keeps_misses() -> None:
    [kept] = saved_agents.merge([entry("t1", misses=1)], [], True)
    assert kept.misses == 1


def test_a_failed_restore_entry_is_kept_unchanged_however_often_it_is_missed() -> None:
    failed = entry("t1", misses=1, restore_failed=True)
    assert saved_agents.merge([failed], [], False) == [failed]
    assert saved_agents.merge([failed], [], True) == [failed]


def test_a_failed_restore_entry_that_is_seen_is_replaced_by_what_is_seen() -> None:
    [merged] = saved_agents.merge([entry("t1", restore_failed=True)], [entry("t1")], False)
    assert merged.restore_failed is False


@pytest.mark.parametrize(
    "content", [b"{", b"", b'{"version": 99, "agents": [{"unknown": 1}], "extra": true}']
)
def test_corrupt_file_reads_as_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture, content: bytes) -> None:
    path = tmp_path / "agents.json"
    path.write_bytes(content)
    assert saved_agents.load(path) == SavedAgents()
    assert len(caplog.records) == 1


def newer_file(path: Path) -> bytes:
    """A file a newer agent-bridge wrote: valid agents, a higher version."""
    content = SavedAgents(agents=[entry("t1")]).model_copy(update={"version": 2}).model_dump_json().encode()
    path.write_bytes(content)
    return content


def test_a_newer_agent_bridges_file_reads_as_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "agents.json"
    newer_file(path)
    assert saved_agents.load(path) == SavedAgents()
    assert "newer agent-bridge" in caplog.text


def test_writers_never_overwrite_a_newer_agent_bridges_file(tmp_path: Path) -> None:
    # A downgrade must not wipe what the newer version saved.
    path = tmp_path / "agents.json"
    content = newer_file(path)
    with pytest.raises(StateError):
        saved_agents.forget(path, "t1")
    assert path.read_bytes() == content


def test_writers_never_overwrite_a_file_they_couldnt_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "agents.json"
    saved_agents.save(path, SavedAgents(agents=[entry("t1"), entry("t2")]))
    content = path.read_bytes()
    real_read = Path.read_bytes

    def denied(self: Path) -> bytes:
        if self == path:
            raise PermissionError(13, "Permission denied")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(StateError):
        saved_agents.forget(path, "t1")
    monkeypatch.undo()
    assert path.read_bytes() == content


def test_writers_replace_a_corrupt_file(tmp_path: Path) -> None:
    # Nothing in it can be restored, so writing over it is how it recovers.
    path = tmp_path / "agents.json"
    path.write_bytes(b"{")
    saved_agents.forget(path, "t1")
    assert saved_agents.load(path) == SavedAgents()


def test_missing_file_reads_as_empty_without_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert saved_agents.load(tmp_path / "agents.json") == SavedAgents()
    assert not caplog.records


def test_unknown_fields_in_a_version_1_file_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    path.write_text('{"version": 1, "future": 1, "agents": []}')
    assert saved_agents.load(path) == SavedAgents()


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    saved = SavedAgents(agents=[entry("t1", misses=1)])
    saved_agents.save(path, saved)
    assert saved_agents.load(path) == saved


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_save_is_private(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    saved_agents.save(path, SavedAgents())
    assert path.stat().st_mode & 0o777 == 0o600


def test_agents_file_is_beside_state_file() -> None:
    assert saved_agents.agents_file().name == "agents.json"
    assert saved_agents.agents_file().parent == saved_agents.state_file().parent


def test_lock_is_removed_on_exit(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    with saved_agents.locked(path):
        assert path.with_name("agents.json.lock").exists()
    assert not path.with_name("agents.json.lock").exists()


def test_stale_lock_is_taken_over(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    lock = path.with_name("agents.json.lock")
    lock.touch()
    old = time.time() - 60
    os.utime(lock, (old, old))
    with saved_agents.locked(path):
        pass
    assert not lock.exists()


def test_forget_removes_by_terminal_id(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    saved_agents.save(path, SavedAgents(agents=[entry("t1"), entry("t2")]))
    saved_agents.forget(path, "t1")
    assert [saved.terminal_id for saved in saved_agents.load(path).agents] == ["t2"]
