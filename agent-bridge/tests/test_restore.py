import asyncio
import os
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient
from fakes import FakeRunner, err, ok

from agent_bridge.agents import AgentService
from agent_bridge.config import Settings
from agent_bridge.herdr import Herdr
from agent_bridge import restore as restore_module
from agent_bridge.app import create_app
from agent_bridge.herdr import HerdrError
from agent_bridge.restore import restore, watch
from agent_bridge.saved_agents import SavedAgent, SavedAgents, load, save

TOKEN = "t" * 32
UUID = "11111111-2222-3333-4444-555555555555"
OTHER_UUID = "99999999-2222-3333-4444-555555555555"


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    return tmp_path / "webapp"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "agents.json"


@pytest.fixture(autouse=True)
def environ(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The environment every live Claude in these tests reports; a test may fill it."""
    found: dict[str, str] = {}
    monkeypatch.setattr(psutil.Process, "environ", lambda self: found)
    return found


def entry(folder: Path, terminal_id: str = "t-old", **fields) -> SavedAgent:
    values = {
        "name": "webapp", "cwd": str(folder), "config_dir": None, "args": ["--permission-mode", "auto"],
        "session_id": None, "pane_id": "w1:p1", "terminal_id": terminal_id, **fields,
    }
    return SavedAgent(**values)


def pane(pane_id: str = "w1:p1", terminal_id: str = "t-new", agent: str | None = "claude", session: str | None = None,
         cwd: str = "/x") -> dict:
    found = {
        "pane_id": pane_id, "terminal_id": terminal_id, "agent": agent, "cwd": cwd,
        "workspace_id": pane_id.split(":")[0], "name": "webapp" if agent else None,
    }
    if session is not None:
        found["agent_session"] = {"agent": "claude", "kind": "id", "source": "herdr:claude", "value": session}
    return found


def process_info(argv: list[str]) -> tuple[int, str, str]:
    pid = os.getpid()
    claude = {"pid": pid, "name": "claude", "argv0": "claude", "argv": argv}
    return ok({"process_info": {"foreground_process_group_id": pid, "foreground_processes": [claude]}})


def started(n: int) -> dict:
    return {"agent": "claude", "name": "webapp", "pane_id": f"w9{n}:p1", "terminal_id": f"t9{n}",
            "cwd": "/x", "workspace_id": f"w9{n}"}


def runner_for(panes: list[dict], argv: list[str] | None = None, starts: int = 3, **overrides) -> FakeRunner:
    """herdr with `panes` (its agents are those with an agent kind) and room for `starts`
    agent starts, each in a new workspace."""
    responses = {
        ("pane", "list"): ok({"panes": panes}),
        ("agent", "list"): ok({"agents": [found for found in panes if found["agent"]]}),
        ("pane", "process-info"): process_info(argv or ["claude"]),
        ("pane", "close"): ok({"type": "ok"}),
        ("workspace", "create"): [
            ok({"workspace": {"workspace_id": f"w9{n}"}, "root_pane": {"pane_id": f"w9{n}:p1"}})
            for n in range(starts)
        ],
        ("agent", "start"): [ok({"agent": started(n)}) for n in range(starts)],
        ("pane", "wait-output"): err("timeout"),
        ("workspace", "close"): ok({"type": "ok"}),
    }
    responses.update({tuple(key.split()): value for key, value in overrides.items()})
    return FakeRunner(responses)


def run(runner: FakeRunner, path: Path, saved: list[SavedAgent], tmp_path: Path, **settings) -> list[str]:
    save(path, SavedAgents(agents=saved))
    settings.setdefault("allowed_roots", (tmp_path.resolve(),))
    herdr = Herdr(runner=runner)
    service = AgentService(Settings(token=TOKEN, **settings), herdr)
    lines = asyncio.run(restore(service, herdr, path))
    assert len(lines) == len(saved)
    # A restored agent picks up where it was and waits: it is never sent a prompt.
    assert runner.called("agent", "prompt") == []
    return lines


def claude_args(runner: FakeRunner, index: int = 0) -> list[str]:
    start = runner.called("agent", "start")[index]
    return start[start.index("--") + 1 :]


def test_survivor_is_skipped(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([pane(terminal_id="t-old")])
    saved = [entry(folder)]
    [line] = run(runner, path, saved, tmp_path)
    assert "running" in line
    assert runner.called("agent", "start") == runner.called("pane", "close") == []
    assert load(path).agents == saved


def test_herdr_resume_with_same_account_and_args_is_kept(
    path: Path, folder: Path, tmp_path: Path, environ: dict[str, str]
) -> None:
    environ["CLAUDE_CONFIG_DIR"] = "/cfg"
    runner = runner_for([pane(session=UUID)], ["claude", "--permission-mode", "auto", "--resume", UUID])
    run(runner, path, [entry(folder, config_dir="/cfg", session_id=UUID)], tmp_path)
    assert runner.called("agent", "start") == runner.called("pane", "close") == []


def test_herdr_resume_with_other_account_is_replaced(path: Path, folder: Path, tmp_path: Path) -> None:
    # herdr's own resume runs `claude --resume <id>`: no CLAUDE_CONFIG_DIR, no arguments.
    runner = runner_for([pane(session=UUID)], ["claude", "--resume", UUID])
    run(runner, path, [entry(folder, config_dir="/cfg", session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == [["pane", "close", "w1:p1"]]
    [create] = runner.called("workspace", "create")
    assert runner.calls.index(["pane", "close", "w1:p1"]) < runner.calls.index(create)
    assert create[create.index("--env") + 1] == f"CLAUDE_CONFIG_DIR={Path('/cfg')}"
    assert claude_args(runner) == ["--permission-mode", "auto", "--resume", UUID]


def test_herdr_resume_with_other_args_is_replaced(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([pane(session=UUID)], ["claude", "--resume", UUID])
    run(runner, path, [entry(folder, session_id=UUID)], tmp_path)
    assert runner.called("pane", "close") == [["pane", "close", "w1:p1"]]
    assert claude_args(runner) == ["--permission-mode", "auto", "--resume", UUID]


def test_shell_pane_is_closed_and_agent_started_with_resume(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([pane(agent=None)])
    [line] = run(runner, path, [entry(folder, session_id=UUID, config_dir="/cfg")], tmp_path)

    assert runner.called("pane", "close") == [["pane", "close", "w1:p1"]]
    [start] = runner.called("agent", "start")
    assert start[2] == "webapp"
    assert claude_args(runner) == ["--permission-mode", "auto", "--resume", UUID]
    [create] = runner.called("workspace", "create")
    assert create[create.index("--cwd") + 1] == str(folder)
    assert "restored" in line
    # The entry now names the new agent, still with its conversation.
    [replaced] = load(path).agents
    assert (replaced.pane_id, replaced.terminal_id, replaced.session_id, replaced.misses) == ("w90:p1", "t90", UUID, 0)
    assert replaced.restore_failed is False
    assert (replaced.name, replaced.cwd, replaced.config_dir, replaced.args) == (
        "webapp", str(folder), "/cfg", ["--permission-mode", "auto"],
    )


def test_an_agent_pane_at_the_saved_pane_id_is_left_alone(path: Path, folder: Path, tmp_path: Path) -> None:
    # Another agent took the pane id: only a plain shell is closed.
    runner = runner_for([pane(session=OTHER_UUID)])
    run(runner, path, [entry(folder, session_id=UUID)], tmp_path)
    assert runner.called("pane", "close") == []
    assert len(runner.called("agent", "start")) == 1


def test_no_id_alone_in_folder_uses_continue(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([])
    other = entry(tmp_path / "api", terminal_id="t-api", pane_id="w2:p1", session_id=UUID)
    run(runner, path, [entry(folder), other], tmp_path)
    assert claude_args(runner, 0) == ["--permission-mode", "auto", "--continue"]
    [replaced, _] = load(path).agents
    assert replaced.session_id is None


def test_no_id_sharing_folder_starts_fresh(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([])
    with_id = entry(folder, session_id=UUID)
    without = entry(folder, terminal_id="t-old-2", pane_id="w2:p1")
    lines = run(runner, path, [with_id, without], tmp_path)

    assert claude_args(runner, 0) == ["--permission-mode", "auto", "--resume", UUID]
    assert claude_args(runner, 1) == ["--permission-mode", "auto"]
    assert f"fresh conversation: another saved agent shares {folder} and its account" in lines[1]
    assert [agent.terminal_id for agent in load(path).agents] == ["t90", "t91"]


def test_no_id_in_a_shared_folder_with_another_account_continues(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([])
    run(runner, path, [entry(folder), entry(folder, terminal_id="t-old-2", config_dir="/cfg")], tmp_path)
    assert claude_args(runner, 0)[-1] == claude_args(runner, 1)[-1] == "--continue"


def test_no_id_with_unknown_config_sharing_its_accounts_folder_starts_fresh(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # The unknown one starts on the folder's account, the other's: --continue could pick
    # the conversation the other resumes.
    folder.mkdir()
    personal = tmp_path / "cfg-personal"
    runner = runner_for([])
    accounts = {"personal": {"config_dir": personal, "folders": [folder]}}
    known = entry(folder, config_dir=str(personal), session_id=UUID)
    unknown = entry(folder, terminal_id="t-old-2", pane_id="w2:p1", config_dir_unknown=True)
    lines = run(runner, path, [known, unknown], tmp_path, accounts=accounts)

    assert claude_args(runner, 1) == ["--permission-mode", "auto"]
    assert "fresh conversation" in lines[1]
    create = runner.called("workspace", "create")[1]
    assert create[create.index("--env") + 1] == f"CLAUDE_CONFIG_DIR={personal}"


def test_with_id_sharing_folder_still_resumes(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([])
    first = entry(folder, session_id=UUID)
    second = entry(folder, terminal_id="t-old-2", pane_id="w2:p1", session_id=OTHER_UUID)
    run(runner, path, [first, second], tmp_path)
    assert claude_args(runner, 0)[-2:] == ["--resume", UUID]
    assert claude_args(runner, 1)[-2:] == ["--resume", OTHER_UUID]


def test_untrusted_folder_drops_entry(path: Path, folder: Path, tmp_path: Path) -> None:
    trust_dialog = ok({"matched_line": "Yes, I trust this folder"})
    runner = runner_for([], **{"pane wait-output": trust_dialog})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)
    assert "trust" in line
    assert load(path).agents == []


def test_other_failure_keeps_entry(path: Path, folder: Path, tmp_path: Path) -> None:
    failing = entry(folder, session_id=UUID)
    second = entry(tmp_path / "api", terminal_id="t-api", pane_id="w2:p1", session_id=OTHER_UUID)
    runner = runner_for([], **{"agent start": [err("agent_start_failed"), ok({"agent": started(1)})]})
    lines = run(runner, path, [failing, second], tmp_path)

    assert "agent_start_failed" in lines[0]
    # Kept as it was, for the next restart; the next entry is still restored.
    assert [agent.terminal_id for agent in load(path).agents] == ["t-old", "t91"]
    # Marked, so the snapshots neither count it a miss nor let it set off a restore.
    assert load(path).agents[0] == failing.model_copy(update={"restore_failed": True})


def test_a_started_agent_is_no_longer_marked_failed(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([])
    run(runner, path, [entry(folder, session_id=UUID, restore_failed=True)], tmp_path)
    [replaced] = load(path).agents
    assert (replaced.terminal_id, replaced.restore_failed) == ("t90", False)


def test_restore_skips_agent_limit_and_roots(path: Path, folder: Path, tmp_path: Path) -> None:
    running = pane("w3:p1", terminal_id="t-running", session=OTHER_UUID)
    runner = runner_for([running])
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    run(runner, path, [entry(folder, session_id=UUID)], tmp_path, max_agents=1, allowed_roots=(allowed,))
    assert len(runner.called("agent", "start")) == 1


def test_remote_control_entry_keeps_flag_and_name(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([])
    saved = entry(folder, name="webapp", args=["--remote-control", "webapp"], session_id=UUID)
    run(runner, path, [saved], tmp_path)

    [start] = runner.called("agent", "start")
    assert start[2] == "webapp"
    assert claude_args(runner) == ["--remote-control", "webapp", "--resume", UUID]
    # Watched for, as start_agent does, so a blocked Remote Control is noticed.
    probe = runner.called("pane", "wait-output")[0]
    assert "Remote Control is disabled" in probe[probe.index("--regex") + 1]


def test_restored_name_avoids_running_ones_and_falls_back_to_the_folder(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    runner = runner_for([pane("w3:p1", terminal_id="t-running", session=OTHER_UUID)])
    run(runner, path, [entry(folder, name=None, session_id=UUID)], tmp_path)
    # The running agent is named webapp; the folder is too.
    assert runner.called("agent", "start")[0][2] == "webapp-2"


def test_unknown_config_uses_the_folders_account(path: Path, folder: Path, tmp_path: Path) -> None:
    folder.mkdir()
    personal = tmp_path / "cfg-personal"
    runner = runner_for([])
    accounts = {"personal": {"config_dir": personal, "folders": [folder]}}
    run(runner, path, [entry(folder, config_dir_unknown=True, session_id=UUID)], tmp_path, accounts=accounts)
    [create] = runner.called("workspace", "create")
    assert create[create.index("--env") + 1] == f"CLAUDE_CONFIG_DIR={personal}"


def test_unknown_config_outside_every_account_uses_the_bridges(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([])
    config_dir = tmp_path / "cfg-agents"
    saved = [entry(folder, config_dir_unknown=True, session_id=UUID)]
    run(runner, path, saved, tmp_path, claude_config_dir=config_dir)
    [create] = runner.called("workspace", "create")
    assert create[create.index("--env") + 1] == f"CLAUDE_CONFIG_DIR={config_dir}"


def test_restore_never_prompts(path: Path, folder: Path, tmp_path: Path) -> None:
    # run() checks it after every restore in this file; here for every way an entry goes.
    runner = runner_for([pane(terminal_id="t-old"), pane("w2:p1", agent=None, terminal_id="t-shell")])
    saved = [
        entry(folder),
        entry(tmp_path / "api", terminal_id="t-api", pane_id="w2:p1", session_id=UUID),
        entry(tmp_path / "docs", terminal_id="t-docs", pane_id="w4:p1"),
    ]
    run(runner, path, saved, tmp_path)
    assert len(runner.called("agent", "start")) == 2


class Rounds:
    """Runs `watch` for as many rounds as `snapshots` needs, each answer an exception to
    raise or the agents herdr lists, with `restore` replaced and herdr's panes given per
    round."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, path: Path, snapshots: list, panes: list[list[str]]) -> None:
        self.path, self.snapshots, self.panes = path, list(snapshots), list(panes)
        self.restores: list[int] = []
        # The file as each round left it, None while there isn't one.
        self.files: list[list[SavedAgent] | None] = []
        self.round = 1
        self.rounds = 1
        monkeypatch.setattr(restore_module, "snapshot", self.snapshot)
        monkeypatch.setattr(restore_module, "restore", self.restore)

    async def snapshot(self, herdr) -> list[SavedAgent]:
        answer = self.snapshots.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def restore(self, service, herdr, path: Path) -> list[str]:
        self.restores.append(self.round)
        return []

    async def list_panes(self) -> list[dict]:
        return [{"pane_id": "w1:p1", "terminal_id": terminal} for terminal in self.panes.pop(0)]

    async def sleep(self, seconds: float) -> None:
        self.files.append(load(self.path).agents if self.path.exists() else None)
        if self.round == self.rounds:
            raise asyncio.CancelledError
        self.round += 1

    def watch(self, rounds: int) -> None:
        self.rounds = rounds
        herdr = type("FakeHerdr", (), {"list_panes": self.list_panes})()
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(watch(None, herdr, self.path, sleep=self.sleep))


def test_first_round_restores_then_saves(monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path) -> None:
    seen = entry(folder, terminal_id="t-new")
    rounds = Rounds(monkeypatch, path, [[], [seen]], [])
    rounds.watch(1)
    assert rounds.restores == [1]
    assert rounds.files == [[seen]]


def test_unreachable_then_reachable_restores_once(monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path) -> None:
    seen = entry(folder, terminal_id="t1")
    down = HerdrError("server_down", "herdr is down")
    # Rounds 1 and 2 fail; round 3 restores (two snapshots); round 4 finds t1 among the panes.
    rounds = Rounds(monkeypatch, path, [down, down, [], [seen], [seen]], [["t1"]])
    rounds.watch(4)
    assert rounds.restores == [3]
    # Nothing is written while herdr doesn't answer.
    assert rounds.files[:2] == [None, None]
    assert rounds.files[2:] == [[seen], [seen]]


def test_all_terminal_ids_new_counts_as_restart(monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path) -> None:
    old = entry(folder, terminal_id="t-old")
    new = entry(folder, terminal_id="t-new")
    # Round 1 saves the old agent; in round 2 herdr's panes are all new ones.
    rounds = Rounds(monkeypatch, path, [[old], [old], [new], [new]], [["t-new"]])
    rounds.watch(2)
    assert rounds.restores == [1, 2]


def test_same_server_does_not_restore_again(monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path) -> None:
    seen = entry(folder, terminal_id="t1")
    rounds = Rounds(monkeypatch, path, [[seen], [seen], [seen]], [["t1"], ["t1"]])
    rounds.watch(3)
    assert rounds.restores == [1]


def test_failed_entries_do_not_set_off_a_restore(monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path) -> None:
    failed = entry(folder, terminal_id="t-failed", restore_failed=True)
    save(path, SavedAgents(agents=[failed]))
    # A failed entry keeps a stale terminal id: it alone is no sign of a new server.
    rounds = Rounds(monkeypatch, path, [[], [], [], []], [])
    rounds.watch(3)
    assert rounds.restores == [1]
    assert rounds.files[-1] == [failed]


def test_a_live_entry_among_failed_ones_still_detects_a_restart(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path
) -> None:
    failed = entry(folder, terminal_id="t-failed", restore_failed=True)
    running = entry(folder, terminal_id="t-old")
    save(path, SavedAgents(agents=[failed, running]))
    rounds = Rounds(monkeypatch, path, [[], [], [running], [running]], [["t-other"]])
    rounds.watch(2)
    assert rounds.restores == [1, 2]


def test_a_round_that_fails_unexpectedly_does_not_end_the_watching(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path, caplog: pytest.LogCaptureFixture
) -> None:
    seen = entry(folder, terminal_id="t1")
    rounds = Rounds(monkeypatch, path, [RuntimeError("boom"), [], [seen]], [])
    rounds.watch(2)
    assert rounds.restores == [2]
    assert rounds.files == [None, [seen]]
    assert "boom" in caplog.text


def test_watch_disabled_by_setting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    started: list[Path] = []

    async def fake_watch(service, herdr, path, *args, **kwargs) -> None:
        started.append(path)
        await asyncio.sleep(3600)

    monkeypatch.setattr("agent_bridge.app.watch", fake_watch)
    monkeypatch.setenv("AGENT_BRIDGE_RESTORE_AGENTS", "false")
    herdr = Herdr(runner=FakeRunner({}))
    for restore_agents, expected in ((False, 0), (True, 1)):
        started.clear()
        settings = Settings(token=TOKEN, allowed_roots=(tmp_path.resolve(),), restore_agents=restore_agents)
        with TestClient(create_app(settings, herdr)):
            pass
        assert len(started) == expected
    # The environment is read when no value is passed.
    assert Settings(token=TOKEN, allowed_roots=(tmp_path.resolve(),)).restore_agents is False
