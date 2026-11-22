import asyncio
import contextlib
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

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
from agent_bridge.saved_agents import SavedAgent, SavedAgents, agents_file, load, save
from agent_bridge.state import StateError

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
def conversations(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Session ids that have no conversation on disk; by default every one has. Keeps the
    tests off the real ~/.claude."""
    missing: set[str] = set()
    monkeypatch.setattr(restore_module, "_has_conversation",
                        lambda config_dir, session_id: session_id not in missing)
    return missing


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


def test_an_agent_never_prompted_starts_fresh_under_its_session_id(
    path: Path, folder: Path, tmp_path: Path, conversations: set[str]
) -> None:
    # Claude Code writes no conversation until the first prompt, and refuses to --resume one
    # it doesn't have ("No conversation found with session ID").
    conversations.add(UUID)
    runner = runner_for([])
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert claude_args(runner) == ["--session-id", UUID, "--permission-mode", "auto"]
    assert "no conversation yet" in line
    [replaced] = load(path).agents
    assert (replaced.session_id, replaced.restore_failed) == (UUID, False)


def test_has_conversation_looks_in_the_accounts_projects(tmp_path: Path) -> None:
    config = tmp_path / "cfg"
    (config / "projects" / "-work-webapp").mkdir(parents=True)
    (config / "projects" / "-work-webapp" / f"{UUID}.jsonl").write_text("{}\n")

    assert restore_module._has_conversation_on_disk(config, UUID) is True
    assert restore_module._has_conversation_on_disk(config, OTHER_UUID) is False
    assert restore_module._has_conversation_on_disk(tmp_path / "missing", UUID) is False


def test_herdr_resume_on_the_default_config_is_replaced_for_an_unknown_accounts_agent(
    path: Path, folder: Path, tmp_path: Path, environ: dict[str, str]
) -> None:
    # Its account couldn't be read when saved, so it starts on its folder's account; herdr's
    # own resume runs on the default config: not "as it was".
    folder.mkdir()
    work = tmp_path / "cfg-work"
    environ.pop("CLAUDE_CONFIG_DIR", None)
    runner = runner_for([pane(session=UUID, cwd=str(folder))], ["claude", "--resume", UUID])
    accounts = {"work": {"config_dir": work, "folders": [folder]}}
    saved = entry(folder, config_dir_unknown=True, session_id=UUID, args=[])
    run(runner, path, [saved], tmp_path, accounts=accounts)

    assert runner.called("pane", "close") == [["pane", "close", "w1:p1"]]
    [create] = runner.called("workspace", "create")
    assert create[create.index("--env") + 1] == f"CLAUDE_CONFIG_DIR={work}"


def test_herdr_resume_with_other_account_is_replaced(path: Path, folder: Path, tmp_path: Path) -> None:
    # herdr's own resume runs `claude --resume <id>`: no CLAUDE_CONFIG_DIR, no arguments.
    runner = runner_for([pane(session=UUID)], ["claude", "--resume", UUID])
    [line] = run(runner, path, [entry(folder, config_dir="/cfg", session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == [["pane", "close", "w1:p1"]]
    assert line.endswith(f"resuming {UUID} (closed herdr's own resume in w1:p1)")
    [create] = runner.called("workspace", "create")
    assert runner.calls.index(["pane", "close", "w1:p1"]) < runner.calls.index(create)
    assert create[create.index("--env") + 1] == f"CLAUDE_CONFIG_DIR={Path('/cfg')}"
    assert claude_args(runner) == ["--resume", UUID, "--permission-mode", "auto"]


def test_a_busy_pane_running_its_conversation_is_never_closed(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # The user resumed it by hand, with other arguments, and is in the middle of a turn.
    busy = pane(session=UUID, cwd=str(folder)) | {"agent_status": "working"}
    runner = runner_for([busy], ["claude", "--resume", UUID])
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == []
    assert runner.called("agent", "start") == []
    assert "busy" in line
    # Taken as the agent's own pane from now on.
    [kept] = load(path).agents
    assert (kept.pane_id, kept.terminal_id, kept.misses) == ("w1:p1", "t-new", 0)


def test_herdr_resume_with_other_args_is_replaced(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([pane(session=UUID)], ["claude", "--resume", UUID])
    run(runner, path, [entry(folder, session_id=UUID)], tmp_path)
    assert runner.called("pane", "close") == [["pane", "close", "w1:p1"]]
    assert claude_args(runner) == ["--resume", UUID, "--permission-mode", "auto"]


def test_shell_pane_is_closed_and_agent_started_with_resume(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([pane(agent=None, cwd=str(folder))])
    [line] = run(runner, path, [entry(folder, session_id=UUID, config_dir="/cfg")], tmp_path)

    assert runner.called("pane", "close") == [["pane", "close", "w1:p1"]]
    [start] = runner.called("agent", "start")
    assert start[2] == "webapp"
    assert claude_args(runner) == ["--resume", UUID, "--permission-mode", "auto"]
    [create] = runner.called("workspace", "create")
    assert create[create.index("--cwd") + 1] == str(folder)
    assert line.endswith(f"resuming {UUID} (closed the shell left in w1:p1)")
    # The entry now names the new agent, still with its conversation.
    [replaced] = load(path).agents
    assert (replaced.pane_id, replaced.terminal_id, replaced.session_id, replaced.misses) == ("w90:p1", "t90", UUID, 0)
    assert replaced.restore_failed is False
    assert (replaced.name, replaced.cwd, replaced.config_dir, replaced.args) == (
        "webapp", str(folder), "/cfg", ["--permission-mode", "auto"],
    )


def test_a_restored_shell_keeping_the_session_id_is_closed_as_a_shell(path: Path, folder: Path, tmp_path: Path) -> None:
    # With herdr's resume off, its restored panes are shells that still carry the saved
    # session id: the log must not call that herdr's own resume.
    runner = runner_for([pane(agent=None, session=UUID, cwd=str(folder))])
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == [["pane", "close", "w1:p1"]]
    assert line.endswith(f"resuming {UUID} (closed the shell left in w1:p1)")


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
    assert claude_args(runner, 0) == ["--continue", "--permission-mode", "auto"]
    [replaced, _] = load(path).agents
    assert replaced.session_id is None


def test_no_id_sharing_folder_starts_fresh(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([])
    with_id = entry(folder, session_id=UUID)
    without = entry(folder, terminal_id="t-old-2", pane_id="w2:p1")
    lines = run(runner, path, [with_id, without], tmp_path)

    assert claude_args(runner, 0) == ["--resume", UUID, "--permission-mode", "auto"]
    assert claude_args(runner, 1) == ["--permission-mode", "auto"]
    assert f"fresh conversation: another saved agent shares {folder} and its account" in lines[1]
    assert [agent.terminal_id for agent in load(path).agents] == ["t90", "t91"]


def test_no_id_in_a_shared_folder_with_another_account_continues(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([])
    run(runner, path, [entry(folder), entry(folder, terminal_id="t-old-2", config_dir="/cfg")], tmp_path)
    assert claude_args(runner, 0)[0] == claude_args(runner, 1)[0] == "--continue"


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
    assert claude_args(runner, 0)[:2] == ["--resume", UUID]
    assert claude_args(runner, 1)[:2] == ["--resume", OTHER_UUID]


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


def test_a_failed_restore_is_kept_for_the_next_restart_even_after_a_miss(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # One save missed it, then herdr brought its pane back; the start fails.
    runner = runner_for([pane(agent=None, cwd=str(folder))], **{"agent start": err("agent_start_failed")})
    run(runner, path, [entry(folder, session_id=UUID, misses=1)], tmp_path)

    # Its miss doesn't count against it any more: at the next restart, with that shell gone,
    # it must be tried again, not forgotten as "already gone".
    [kept] = load(path).agents
    assert (kept.restore_failed, kept.misses) == (True, 0)


def test_a_file_that_cant_be_written_doesnt_stop_the_restore(
    path: Path, folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = entry(folder, session_id=UUID)
    second = entry(tmp_path / "api", terminal_id="t-api", pane_id="w2:p1", session_id=OTHER_UUID)

    def unwritable(*args: object) -> None:
        raise StateError(f"Can't write {path} (No space left on device).")

    monkeypatch.setattr(restore_module, "_replace", unwritable)
    runner = runner_for([])
    lines = run(runner, path, [first, second], tmp_path)

    # Both are started, and the run ends normally, so the watcher doesn't start them again.
    assert len(runner.called("agent", "start")) == 2
    assert all("No space left on device" in line for line in lines)


def test_the_watcher_leaves_out_an_agent_stopped_while_it_waited_for_the_file(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path
) -> None:
    gone = entry(folder, terminal_id="t-gone")
    rounds = Rounds(monkeypatch, path, [[], [gone]], [])
    real_locked = restore_module.locked

    @contextlib.contextmanager
    def stopped_meanwhile(locked_path: Path):
        # stop_agent marks the agent stopped and forgets it just before the watcher gets the lock.
        rounds.service.stopped.add("t-gone")
        with real_locked(locked_path):
            yield

    monkeypatch.setattr(restore_module, "locked", stopped_meanwhile)
    rounds.watch(1)
    assert rounds.files == [[]]


def test_a_started_agent_is_no_longer_marked_failed(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([])
    run(runner, path, [entry(folder, session_id=UUID, restore_failed=True)], tmp_path)
    [replaced] = load(path).agents
    assert (replaced.terminal_id, replaced.restore_failed) == ("t90", False)


def test_an_entry_that_had_already_gone_is_not_restored(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([])
    [line] = run(runner, path, [entry(folder, misses=1)], tmp_path)
    assert "not restored, and forgotten: it had already gone" in line
    assert runner.called("agent", "start") == runner.called("workspace", "create") == []
    # Forgotten now: its stale pane id can't match a pane a later herdr server hands out.
    assert load(path).agents == []


def test_a_pane_with_its_id_in_another_folder_isnt_taken_for_its_shell(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # A herdr server that lost its session numbers panes afresh: the user's own shell in
    # another folder can have the saved pane id.
    other = pane(agent=None, cwd=str(tmp_path / "elsewhere"))
    runner = runner_for([other])

    [line] = run(runner, path, [entry(folder, session_id=UUID, misses=1)], tmp_path)

    assert runner.called("pane", "close") == []
    assert runner.called("agent", "start") == []
    assert "forgotten: it had already gone" in line


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
    assert claude_args(runner) == ["--resume", UUID, "--remote-control", "webapp"]
    # Watched for, as start_agent does, so a blocked Remote Control is noticed.
    probe = runner.called("pane", "wait-output")[0]
    assert "Remote Control is disabled" in probe[probe.index("--regex") + 1]


def test_a_trailing_flag_with_an_optional_value_does_not_take_the_resume(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    runner = runner_for([])
    run(runner, path, [entry(folder, args=["--remote-control"])], tmp_path)
    # After it, --continue would be Remote Control's name.
    assert claude_args(runner) == ["--continue", "--remote-control"]


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
        self.restore_errors: list[Exception] = []
        # What AgentService.stopped is to watch.
        self.service = SimpleNamespace(stopped=set())
        monkeypatch.setattr(restore_module, "snapshot", self.snapshot)
        monkeypatch.setattr(restore_module, "restore", self.restore)

    async def snapshot(self, herdr) -> list[SavedAgent]:
        answer = self.snapshots.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def restore(self, service, herdr, path: Path) -> list[str]:
        self.restores.append(self.round)
        if self.restore_errors:
            raise self.restore_errors.pop(0)
        return []

    async def list_panes(self) -> list[dict]:
        # A terminal id alone is in pane w1:p1; a pair is a pane id and its terminal id.
        return [
            {"pane_id": "w1:p1", "terminal_id": pane} if isinstance(pane, str)
            else {"pane_id": pane[0], "terminal_id": pane[1]}
            for pane in self.panes.pop(0)
        ]

    async def sleep(self, seconds: float) -> None:
        self.files.append(load(self.path).agents if self.path.exists() else None)
        if self.round == self.rounds:
            raise asyncio.CancelledError
        self.round += 1

    def watch(self, rounds: int) -> None:
        self.rounds = rounds
        herdr = type("FakeHerdr", (), {"list_panes": self.list_panes})()
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(watch(self.service, herdr, self.path, sleep=self.sleep))


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
    # Rounds 1 and 2 each find a new terminal in the running agent's pane.
    rounds = Rounds(monkeypatch, path, [[], [], [running], [running]], [["t-other"], ["t-other"]])
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


def test_serving_with_restore_off_removes_a_saved_agents_file(tmp_path: Path) -> None:
    # Turned off in the env file rather than by setup: a file kept now would bring back
    # agents long gone once restore is on again.
    path = agents_file()
    save(path, SavedAgents(agents=[entry(tmp_path)]))
    for leftover in (path.with_name(f"{path.name}.tmp"), path.with_name(f"{path.name}.lock")):
        leftover.write_text("")
    settings = Settings(token=TOKEN, allowed_roots=(tmp_path.resolve(),), restore_agents=False)

    with TestClient(create_app(settings, Herdr(runner=FakeRunner({})))):
        pass

    assert sorted(p.name for p in path.parent.iterdir() if p.name.startswith("agents.json")) == []


def test_closing_the_last_agent_pane_by_hand_does_not_restore(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path
) -> None:
    seen = entry(folder, terminal_id="t1")
    # Round 2: the pane is gone entirely, which is no restart.
    rounds = Rounds(monkeypatch, path, [[seen], [seen], []], [[]])
    rounds.watch(2)
    assert rounds.restores == [1]


def test_a_saved_pane_id_with_a_new_terminal_id_is_a_restart(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path
) -> None:
    old, new = entry(folder, terminal_id="t-old"), entry(folder, terminal_id="t-new")
    rounds = Rounds(monkeypatch, path, [[old], [old], [new], [new]], [[("w1:p1", "t-new")]])
    rounds.watch(2)
    assert rounds.restores == [1, 2]


def test_answering_again_with_the_saved_terminals_live_is_not_a_restart(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path
) -> None:
    seen = entry(folder, terminal_id="t1")
    down = HerdrError("server_down", "herdr is down")
    # Round 3 answers after an outage; t1 is still there, in a pane elsewhere.
    rounds = Rounds(monkeypatch, path, [[seen], [seen], down, [seen]], [[("w2:p1", "t1")]])
    rounds.watch(3)
    assert rounds.restores == [1]


def test_answering_again_with_no_saved_terminal_live_is_a_restart(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path
) -> None:
    old, new = entry(folder, terminal_id="t-old"), entry(folder, terminal_id="t-new")
    down = HerdrError("server_down", "herdr is down")
    rounds = Rounds(monkeypatch, path, [[old], [old], down, [new], [new]], [[("w2:p1", "t-new")]])
    rounds.watch(3)
    assert rounds.restores == [1, 3]


def test_an_agent_stopped_during_the_snapshot_is_not_saved_again(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path
) -> None:
    gone = entry(folder, terminal_id="t-gone")
    rounds = Rounds(monkeypatch, path, [[], [gone]], [])
    snapshot = rounds.snapshot

    async def stopped_midway(herdr) -> list[SavedAgent]:
        found = await snapshot(herdr)
        if found:
            # stop_agent closes the pane and forgets the entry while the snapshot reads on.
            rounds.service.stopped.add("t-gone")
        return found

    monkeypatch.setattr(restore_module, "snapshot", stopped_midway)
    rounds.watch(1)
    assert rounds.files == [[]]


def test_the_watcher_drops_a_saved_agent_stopped_after_a_restore_wrote_it(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path
) -> None:
    # The restore wrote the new agent's entry; stop_agent ran before that write, so its
    # forget found nothing to remove.
    restored = entry(folder, terminal_id="t-restored")
    save(path, SavedAgents(agents=[restored]))
    restore_module._write(path, [], True, {"t-restored"})
    assert load(path).agents == []


def test_a_restore_cut_short_by_an_error_runs_again_next_round(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path
) -> None:
    seen = entry(folder, terminal_id="t1")
    rounds = Rounds(monkeypatch, path, [[], [], [seen], [seen]], [[("w1:p1", "t1")]])
    rounds.restore_errors = [TimeoutError("lock held")]
    rounds.watch(2)
    # Round 2 has nothing that signals a restart; the unfinished restore alone brings it.
    assert rounds.restores == [1, 2]


class Host:
    """A herdr whose panes change as agents are started and closed, for running `watch` with
    the real snapshot and restore. Each pane runs `claude` with the arguments it was started
    with; a start of an agent named in `failing` fails."""

    def __init__(
        self, panes: list[dict], argv: dict[str, list[str]] | None = None, failing: frozenset[str] = frozenset()
    ) -> None:
        self.panes = {found["pane_id"]: found for found in panes}
        self.argv = dict(argv or {})
        self.failing = failing
        self.workspaces = 0
        self.started: list[str] = []

    async def __call__(self, argv) -> tuple[int, str, str]:
        args = list(argv[1:])
        if args[:2] == ["pane", "list"]:
            return ok({"panes": list(self.panes.values())})
        if args[:2] == ["agent", "list"]:
            return ok({"agents": [found for found in self.panes.values() if found["agent"]]})
        if args[:2] == ["pane", "process-info"]:
            return process_info(self.argv.get(args[-1], ["claude"]))
        if args[:2] in (["pane", "close"], ["workspace", "close"]):
            self.panes.pop(args[-1], None)
            return ok({"type": "ok"})
        if args[:2] == ["workspace", "create"]:
            self.workspaces += 1
            return ok({"workspace": {"workspace_id": f"w9{self.workspaces}"},
                       "root_pane": {"pane_id": f"w9{self.workspaces}:p1"}})
        if args[:2] == ["agent", "start"]:
            name, pane_id = args[2], args[args.index("--pane") + 1]
            if name in self.failing:
                return err("agent_start_failed")
            claude = args[args.index("--") + 1 :]
            session = claude[claude.index("--resume") + 1] if "--resume" in claude else None
            found = pane(pane_id, terminal_id=f"t9{self.workspaces}", session=session) | {"name": name}
            self.panes[pane_id] = found
            self.argv[pane_id] = ["claude", *claude]
            self.started.append(name)
            return ok({"agent": found})
        if args[:2] == ["pane", "wait-output"]:
            return err("timeout")
        raise AssertionError(f"unexpected herdr call: {args}")

    def close(self, name: str) -> None:
        """The user closes the agent named `name` by hand."""
        [pane_id] = [found["pane_id"] for found in self.panes.values() if found.get("name") == name]
        del self.panes[pane_id]


def watch_host(
    monkeypatch: pytest.MonkeyPatch, host: Host, path: Path, saved: list[SavedAgent], tmp_path: Path, rounds: int,
    between: dict[int, Callable[[], object]] | None = None,
) -> list[int]:
    """Runs `watch` against `host` for `rounds` rounds, calling between[n] after round n; the
    rounds that restored."""
    save(path, SavedAgents(agents=saved))
    herdr = Herdr(runner=host)
    service = AgentService(Settings(token=TOKEN, allowed_roots=(tmp_path.resolve(),)), herdr)
    restores: list[int] = []
    current = [1]
    real_restore = restore_module.restore

    async def counted(*args) -> list[str]:
        restores.append(current[0])
        return await real_restore(*args)

    async def sleep(seconds: float) -> None:
        (between or {}).get(current[0], lambda: None)()
        if current[0] == rounds:
            raise asyncio.CancelledError
        current[0] += 1

    monkeypatch.setattr(restore_module, "restore", counted)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(watch(service, herdr, path, sleep=sleep))
    return restores


def test_no_restore_outcome_keeps_setting_off_restores(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path, tmp_path: Path
) -> None:
    # herdr restarted: each pane it brought back has its old id and a new terminal.
    saved = [
        # Its pane came back as a plain shell: restored.
        entry(folder, name="shell", terminal_id="t1", pane_id="w1:p1", session_id=UUID, args=[]),
        # herdr resumed it itself, with its account and arguments: left running.
        entry(folder, name="resumed", terminal_id="t2", pane_id="w2:p1", session_id=OTHER_UUID, args=[]),
        # Gone already before the restart, its pane too: not restored.
        entry(folder, name="gone", terminal_id="t3", pane_id="w3:p1", misses=1),
        # Its start fails: kept for the next restart.
        entry(folder, name="failing", terminal_id="t4", pane_id="w4:p1", args=[]),
    ]
    resumed = pane("w2:p1", terminal_id="n2", session=OTHER_UUID) | {"name": "resumed"}
    host = Host(
        [pane("w1:p1", terminal_id="n1", agent=None), resumed, pane("w4:p1", terminal_id="n4", agent=None)],
        argv={"w2:p1": ["claude", "--resume", OTHER_UUID]},
        failing=frozenset({"failing"}),
    )
    # After round 2 the user closes by hand the agents that run.
    restores = watch_host(monkeypatch, host, path, saved, tmp_path, 5,
                          {2: lambda: (host.close("shell"), host.close("resumed"))})

    assert restores == [1]
    assert host.started == ["shell"]
    # The closed ones age out; the failed one waits for the next restart.
    assert [(agent.name, agent.restore_failed) for agent in load(path).agents] == [("failing", True)]


def test_an_agent_that_had_gone_comes_back_when_its_pane_does(path: Path, folder: Path, tmp_path: Path) -> None:
    # A snapshot taken while the host shut down missed it, but herdr brought its pane back.
    runner = runner_for([pane(agent=None, cwd=str(folder))])
    [line] = run(runner, path, [entry(folder, session_id=UUID, misses=1)], tmp_path)
    assert "restored" in line
    assert claude_args(runner) == ["--resume", UUID, "--permission-mode", "auto"]


def test_herdr_resumed_as_it_was_takes_the_resumed_panes_ids(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([pane(session=UUID, cwd=str(folder))], ["claude", "--permission-mode", "auto", "--resume", UUID])
    run(runner, path, [entry(folder, session_id=UUID, misses=1)], tmp_path)
    [kept] = load(path).agents
    assert (kept.pane_id, kept.terminal_id, kept.misses, kept.restore_failed) == ("w1:p1", "t-new", 0, False)


def test_a_survivors_pane_is_not_taken_for_herdrs_resume(path: Path, folder: Path, tmp_path: Path) -> None:
    # The survivor runs the conversation the other entry saved too, on another account.
    survivor = entry(folder, terminal_id="t-live", pane_id="w2:p1", session_id=UUID, config_dir="/cfg")
    runner = runner_for([pane("w2:p1", terminal_id="t-live", session=UUID)], ["claude", "--resume", UUID])
    lines = run(runner, path, [entry(folder, session_id=UUID), survivor], tmp_path)
    assert runner.called("pane", "close") == runner.called("agent", "start") == []
    assert "already" in lines[0]
    assert [agent.terminal_id for agent in load(path).agents] == ["t-live"]


def test_a_conversation_is_resumed_once(path: Path, folder: Path, tmp_path: Path) -> None:
    first = entry(folder, session_id=UUID)
    second = entry(tmp_path / "api", terminal_id="t-old-2", pane_id="w2:p1", session_id=UUID)
    lines = run(runner_for([]), path, [first, second], tmp_path)
    assert "restored" in lines[0]
    assert "already" in lines[1]
    assert [agent.terminal_id for agent in load(path).agents] == ["t90"]


def test_first_round_with_a_survivor_does_not_restore_agents_closed_meanwhile(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path
) -> None:
    # Only agent-bridge restarted; while it was down the user closed the second agent.
    live = entry(folder, terminal_id="t1", pane_id="w1:p1")
    closed = entry(folder, terminal_id="t2", pane_id="w2:p1")
    save(path, SavedAgents(agents=[live, closed]))
    rounds = Rounds(monkeypatch, path, [[live]], [[("w1:p1", "t1")]])
    rounds.watch(1)
    assert rounds.restores == []
    assert rounds.files == [[live, closed.model_copy(update={"misses": 1})]]


def test_first_round_with_no_saved_terminal_live_restores(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path
) -> None:
    old, new = entry(folder, terminal_id="t-old", pane_id="w5:p1"), entry(folder, terminal_id="t-new")
    save(path, SavedAgents(agents=[old]))
    rounds = Rounds(monkeypatch, path, [[], [new]], [[("w1:p1", "t-new")]])
    rounds.watch(1)
    assert rounds.restores == [1]
