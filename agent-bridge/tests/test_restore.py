import asyncio
import contextlib
import os
import threading
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
def jobs(monkeypatch: pytest.MonkeyPatch) -> set[int]:
    """Shell pids with a Claude Code among their jobs (one suspended, say); by default none.
    Keeps the tests off the real process table."""
    found: set[int] = set()
    monkeypatch.setattr(restore_module, "_claude_among_jobs", lambda shell_pid: shell_pid in found)
    return found


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
        # As herdr's pane list gives it: every pane has one, a shell's is "unknown".
        "agent_status": "idle" if agent else "unknown",
    }
    if session is not None:
        found["agent_session"] = {"agent": "claude", "kind": "id", "source": "herdr:claude", "value": session}
    return found


def shell_info(running: str | None = None) -> tuple[int, str, str]:
    """process-info of a plain shell: idle (its only foreground process is the shell), or
    running `running`, as herdr reports them."""
    process = {"pid": 9000, "name": "zsh", "argv0": "zsh", "argv": ["-zsh"]}
    if running:
        process = {"pid": 9001, "name": running, "argv0": running, "argv": [running]}
    return ok({"process_info": {"foreground_process_group_id": process["pid"],
                                "foreground_processes": [process], "shell_pid": 9000}})


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
        # A pane with no agent is an idle shell, unless a test says otherwise.
        **{("pane", "process-info", "--pane", found["pane_id"]): shell_info() for found in panes if not found["agent"]},
        # An agent's status, read again, is the listed one unless a test says otherwise.
        **{("agent", "get", found["pane_id"]): ok({"agent": found}) for found in panes if found["agent"]},
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


def test_claude_among_jobs_looks_under_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    class Child:
        def __init__(self, name: str) -> None:
            self._name = name

        def name(self) -> str:
            return self._name

    children: list[Child] = []
    monkeypatch.setattr(psutil.Process, "children", lambda self, recursive=False: children)
    assert restore_module._claude_among_jobs_in_os(os.getpid()) is False
    children[:] = [Child("node"), Child("claude.exe")]
    assert restore_module._claude_among_jobs_in_os(os.getpid()) is True
    monkeypatch.setattr(psutil.Process, "children", lambda self, recursive=False: (_ for _ in ()).throw(psutil.AccessDenied()))
    assert restore_module._claude_among_jobs_in_os(os.getpid()) is None


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


def test_herdrs_resume_that_cant_be_closed_is_left_rather_than_run_twice(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # Still running its conversation: a second copy would answer as the first.
    runner = runner_for([pane(session=UUID)], ["claude", "--resume", UUID], **{"pane close": err("close_failed")})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("agent", "start") == []
    assert "couldn't be closed" in line and "left" in line
    # Taken as the agent's own pane from now on, as a busy one is.
    [kept] = load(path).agents
    assert (kept.pane_id, kept.terminal_id) == ("w1:p1", "t-new")


def test_herdrs_resume_gone_though_its_close_failed_is_replaced(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # The close timed out after it went through, or `claude --resume` exited meanwhile.
    # Adopted, the entry would point at a terminal that's gone and age out unrestored.
    resumed = pane(session=UUID)
    runner = runner_for([resumed], ["claude", "--resume", UUID],
                        **{"pane close": err("timeout"), "pane list": [ok({"panes": [resumed]}), ok({"panes": []})]})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert claude_args(runner) == ["--resume", UUID, "--permission-mode", "auto"]
    assert "restored in w90:p1" in line and "couldn't close herdr's own resume in w1:p1" in line
    [restored] = load(path).agents
    assert restored.terminal_id == "t90"


def test_herdrs_resume_that_cant_be_closed_is_left_when_herdr_cant_say_whether_its_gone(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    resumed = pane(session=UUID)
    runner = runner_for([resumed], ["claude", "--resume", UUID],
                        **{"pane close": err("timeout"), "pane list": [ok({"panes": [resumed]}), err("timeout")]})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("agent", "start") == []
    assert "couldn't be closed" in line
    [kept] = load(path).agents
    assert (kept.pane_id, kept.terminal_id) == ("w1:p1", "t-new")


def test_a_leftover_shell_that_cant_be_closed_doesnt_stop_the_start(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    runner = runner_for([pane(agent=None, cwd=str(folder))], **{"pane close": err("close_failed")})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert len(runner.called("agent", "start")) == 1
    assert "couldn't close the shell left in w1:p1" in line


def test_a_pane_running_its_conversation_without_the_hook_is_found_by_its_command_line(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # Resumed by hand in a config without herdr's hook: herdr reports no session, but the
    # command line names it. Not started a second time beside it.
    runner = runner_for([pane()], ["claude", "--resume", UUID])
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == [["pane", "close", "w1:p1"]]
    assert len(runner.called("agent", "start")) == 1
    assert "closed" in line


@pytest.mark.parametrize("pane_id", ["w1:p1", "w5:p1"])
@pytest.mark.parametrize("flag", ["--resume", "--session-id"])
def test_a_pane_herdr_doesnt_know_as_an_agent_yet_running_its_conversation_is_left(
    path: Path, folder: Path, tmp_path: Path, pane_id: str, flag: str
) -> None:
    # Just started, likely on herdr's first answering round: herdr hasn't seen Claude Code in
    # it yet, but its command line names the conversation.
    unclassified = pane(pane_id, agent=None, cwd=str(folder))
    runner = runner_for([unclassified], **{f"pane process-info --pane {pane_id}": process_info(["claude", flag, UUID])})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == runner.called("agent", "start") == []
    assert "left as it is" in line
    [kept] = load(path).agents
    assert (kept.pane_id, kept.terminal_id) == (pane_id, "t-new")


def test_an_agent_pane_without_a_session_whose_process_the_snapshot_missed_is_read_again(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # No session from herdr's hook, and the snapshot couldn't read its process, so nothing
    # said which conversation it runs; its command line, read again, names the agent's.
    working = pane(cwd=str(folder)) | {"agent_status": "working"}
    runner = runner_for([working], **{"pane process-info": [err("timeout"), process_info(["claude", "--resume", UUID])]})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == runner.called("agent", "start") == []
    assert "left as it is" in line
    [kept] = load(path).agents
    assert (kept.pane_id, kept.terminal_id) == ("w1:p1", "t-new")


@pytest.mark.parametrize("status", ["idle", "done"])
def test_an_idle_agent_pane_without_a_session_whose_process_the_snapshot_missed_running_its_conversation_is_replaced(
    path: Path, folder: Path, tmp_path: Path, status: str
) -> None:
    # Read again, its command line resumes the agent's conversation with other arguments:
    # closed and replaced as herdr's own resume is, not started a second time beside it.
    idle = pane(cwd=str(folder)) | {"agent_status": status}
    runner = runner_for([idle], **{"pane process-info": [err("timeout"), process_info(["claude", "--resume", UUID])]})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == [["pane", "close", "w1:p1"]]
    assert claude_args(runner) == ["--resume", UUID, "--permission-mode", "auto"]
    assert "closed herdr's own resume in w1:p1" in line


def test_an_idle_agent_pane_without_a_session_whose_process_the_snapshot_missed_running_it_as_it_was_is_kept(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    idle = pane(cwd=str(folder))
    runner = runner_for([idle], **{"pane process-info": [
        err("timeout"), process_info(["claude", "--permission-mode", "auto", "--resume", UUID])]})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == runner.called("agent", "start") == []
    assert "as it was" in line
    [kept] = load(path).agents
    assert (kept.pane_id, kept.terminal_id) == ("w1:p1", "t-new")


@pytest.mark.parametrize("status", ["working", "idle", "done"])
def test_an_agent_pane_in_its_folder_whose_process_cant_be_read_holds_off_the_start(
    path: Path, folder: Path, tmp_path: Path, status: str
) -> None:
    # It may run the agent's conversation, which a second copy would answer as, whether or
    # not it's in the middle of a turn. Left, and the agent kept for the next restart: if it
    # runs the conversation, the snapshots save it.
    working = pane(cwd=str(folder)) | {"agent_status": status}
    runner = runner_for([working], **{"pane process-info": err("timeout")})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == runner.called("agent", "start") == []
    assert "w1:p1" in line and "couldn't be read" in line
    [kept] = load(path).agents
    assert (kept.terminal_id, kept.restore_failed) == ("t-old", True)


def test_a_held_off_start_still_closes_the_shell_herdr_left(path: Path, folder: Path, tmp_path: Path) -> None:
    # Later restores skip the old pane id of an entry marked failed, so the shell would stay.
    shell = pane("w1:p1", "t-new1", agent=None, cwd=str(folder))
    working = pane("w2:p1", "t-new2", cwd=str(folder)) | {"agent_status": "working"}
    runner = runner_for([shell, working], **{"pane process-info --pane w2:p1": err("timeout")})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == [["pane", "close", "w1:p1"]]
    assert runner.called("agent", "start") == []
    assert "w2:p1" in line and "couldn't be read" in line and "closed the shell left in w1:p1" in line
    [kept] = load(path).agents
    assert (kept.terminal_id, kept.restore_failed) == ("t-old", True)


@pytest.mark.parametrize("unlike", ["folder", "session"])
def test_an_agent_pane_whose_process_cant_be_read_but_cant_run_its_conversation_doesnt_stop_the_start(
    path: Path, folder: Path, tmp_path: Path, unlike: str
) -> None:
    # Elsewhere, where Claude Code doesn't find the conversation; or running another
    # conversation.
    other = pane(cwd=str(tmp_path / "other") if unlike == "folder" else str(folder),
                 session=OTHER_UUID if unlike == "session" else None)
    other["agent_status"] = "working"
    runner = runner_for([other], **{"pane process-info": err("timeout")})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert len(runner.called("agent", "start")) == 1
    assert "restored" in line


@pytest.mark.parametrize("status", ["unknown", None])
def test_a_pane_herdr_cant_classify_running_its_conversation_is_left(
    path: Path, folder: Path, tmp_path: Path, status: str | None
) -> None:
    # herdr can't tell whether it's idle: it may be in the middle of a turn.
    unsure = pane(session=UUID, cwd=str(folder)) | {"agent_status": status}
    runner = runner_for([unsure], ["claude", "--resume", UUID])
    run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == []
    assert runner.called("agent", "start") == []


def test_a_pane_whose_process_cant_be_read_running_its_conversation_is_left(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # Whether it runs as it was can't be told, as with a shell that can't be read.
    idle = pane(session=UUID, cwd=str(folder)) | {"agent_status": "idle"}
    runner = runner_for([idle], **{"pane process-info": err("pane_not_found")})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == []
    assert runner.called("agent", "start") == []
    assert "couldn't be read" in line
    [kept] = load(path).agents
    assert (kept.pane_id, kept.terminal_id) == ("w1:p1", "t-new")


def test_a_pane_whose_account_cant_be_read_running_its_conversation_with_its_args_is_left(
    path: Path, folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Its command line was read, its environment wasn't: it may well run on the agent's account.
    def denied(self: psutil.Process) -> dict[str, str]:
        raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil.Process, "environ", denied)
    runner = runner_for([pane(session=UUID, cwd=str(folder))], ["claude", "--permission-mode", "auto", "--resume", UUID])
    [line] = run(runner, path, [entry(folder, session_id=UUID, config_dir="/cfg")], tmp_path)

    assert runner.called("pane", "close") == runner.called("agent", "start") == []
    assert "couldn't be read" in line
    [kept] = load(path).agents
    assert (kept.pane_id, kept.terminal_id) == ("w1:p1", "t-new")


def test_a_pane_whose_account_cant_be_read_running_its_conversation_with_other_args_is_replaced(
    path: Path, folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(self: psutil.Process) -> dict[str, str]:
        raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil.Process, "environ", denied)
    runner = runner_for([pane(session=UUID, cwd=str(folder))], ["claude", "--resume", UUID])
    run(runner, path, [entry(folder, session_id=UUID, config_dir="/cfg")], tmp_path)

    assert runner.called("pane", "close") == [["pane", "close", "w1:p1"]]
    assert len(runner.called("agent", "start")) == 1


@pytest.mark.parametrize("status", ["working", "blocked"])
def test_a_busy_pane_running_its_conversation_is_never_closed(
    path: Path, folder: Path, tmp_path: Path, status: str
) -> None:
    # The user resumed it by hand, with other arguments, and is in the middle of a turn:
    # working, or waiting for an approval.
    busy = pane(session=UUID, cwd=str(folder)) | {"agent_status": status}
    runner = runner_for([busy], ["claude", "--resume", UUID])
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == []
    assert runner.called("agent", "start") == []
    assert "busy" in line
    # Taken as the agent's own pane from now on.
    [kept] = load(path).agents
    assert (kept.pane_id, kept.terminal_id, kept.misses) == ("w1:p1", "t-new", 0)


def test_a_pane_prompted_while_earlier_agents_restarted_is_never_closed(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # Idle when the panes were listed; the first agent's start takes seconds, and the user
    # prompts the second's resumed pane meanwhile. Closing it would end that turn.
    first = entry(folder, terminal_id="t-a", pane_id="w1:p1", session_id=OTHER_UUID)
    second = entry(folder, terminal_id="t-b", pane_id="w2:p1", session_id=UUID)
    shell = pane("w1:p1", "t-new1", agent=None, cwd=str(folder))
    idle = pane("w2:p1", "t-new2", session=UUID, cwd=str(folder))
    working = idle | {"agent_status": "working"}
    runner = runner_for([shell, idle], ["claude", "--resume", UUID], **{"agent get w2:p1": ok({"agent": working})})
    [_, line] = run(runner, path, [first, second], tmp_path)

    assert len(runner.called("agent", "start")) == 1
    assert ["pane", "close", "w2:p1"] not in runner.calls
    assert "busy in w2:p1" in line
    kept = load(path).agents[1]
    assert (kept.pane_id, kept.terminal_id) == ("w2:p1", "t-new2")


@pytest.mark.parametrize("gone", [False, True])
def test_herdrs_resume_whose_status_cant_be_read_again_is_closed_only_once_gone(
    path: Path, folder: Path, tmp_path: Path, gone: bool
) -> None:
    # herdr can't say whether it's still idle: left, unless its terminal has gone meanwhile.
    resumed = pane(session=UUID)
    runner = runner_for([resumed], ["claude", "--resume", UUID],
                        **{"agent get w1:p1": err("timeout"), "pane list": [ok({"panes": [resumed]}),
                                                                      ok({"panes": [] if gone else [resumed]})]})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == []
    assert len(runner.called("agent", "start")) == int(gone)
    [kept] = load(path).agents
    assert kept.terminal_id == ("t90" if gone else "t-new")
    if not gone:
        assert "left as it is" in line


def _no_agent_answers(resumed: dict) -> dict:
    """How herdr may answer `agent get` for a pane whose Claude Code has exited."""
    shell = {key: value for key, value in resumed.items() if key not in ("agent", "agent_status", "agent_session")}
    return {
        "agent_not_found": err("agent_not_found"),
        "shell": ok({"agent": resumed | {"agent": None, "agent_status": "unknown"}}),
        "no_agent_fields": ok({"agent": shell}),
    }


@pytest.mark.parametrize("answer", ["agent_not_found", "shell", "no_agent_fields"])
def test_herdrs_resume_whose_claude_exited_leaving_an_idle_shell_is_closed_and_replaced(
    path: Path, folder: Path, tmp_path: Path, answer: str
) -> None:
    # herdr's own resume runs plain `claude --resume <id>`, without the account's config:
    # Claude Code can't find the conversation there and exits, leaving a shell. Adopted, the
    # entry would point at a shell no save lists, and age out unrestored.
    resumed = pane(session=UUID, cwd=str(folder))
    runner = runner_for([resumed], ["claude", "--resume", UUID],
                        **{"agent get w1:p1": _no_agent_answers(resumed)[answer],
                           "pane process-info --pane w1:p1": [process_info(["claude", "--resume", UUID]), shell_info()]})
    [line] = run(runner, path, [entry(folder, config_dir="/cfg", session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == [["pane", "close", "w1:p1"]]
    assert claude_args(runner) == ["--resume", UUID, "--permission-mode", "auto"]
    assert line.endswith(f"resuming {UUID} (closed the shell left in w1:p1)")
    [restored] = load(path).agents
    assert (restored.terminal_id, restored.restore_failed) == ("t90", False)


@pytest.mark.parametrize("answer", ["agent_not_found", "shell", "no_agent_fields"])
@pytest.mark.parametrize("left", ["running", "unreadable"])
def test_herdrs_resume_whose_claude_exited_leaving_a_pane_not_known_idle_is_left_and_kept_for_the_next_restart(
    path: Path, folder: Path, tmp_path: Path, answer: str, left: str
) -> None:
    # Something runs in it, or what does can't be read: left. Not adopted either: no save
    # lists a pane herdr doesn't show as an agent, so the entry would age out unrestored.
    resumed = pane(session=UUID, cwd=str(folder))
    now = shell_info("vim") if left == "running" else err("timeout")
    runner = runner_for([resumed], ["claude", "--resume", UUID],
                        **{"agent get w1:p1": _no_agent_answers(resumed)[answer],
                           "pane process-info --pane w1:p1": [process_info(["claude", "--resume", UUID]), now]})
    [line] = run(runner, path, [entry(folder, config_dir="/cfg", session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == runner.called("agent", "start") == []
    assert "kept for the next restart" in line and "w1:p1" in line
    [kept] = load(path).agents
    assert (kept.terminal_id, kept.restore_failed) == ("t-old", True)


def test_herdrs_resume_whose_claude_exited_and_whose_pane_has_gone_is_replaced(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    resumed = pane(session=UUID, cwd=str(folder))
    runner = runner_for([resumed], ["claude", "--resume", UUID],
                        **{"agent get w1:p1": err("agent_not_found"),
                           "pane process-info --pane w1:p1": [process_info(["claude", "--resume", UUID]),
                                                              err("pane_not_found")],
                           "pane list": [ok({"panes": [resumed]}), ok({"panes": []})]})
    run(runner, path, [entry(folder, config_dir="/cfg", session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == []
    assert claude_args(runner) == ["--resume", UUID, "--permission-mode", "auto"]
    [restored] = load(path).agents
    assert restored.terminal_id == "t90"


@pytest.mark.parametrize("by", ["hook", "command line"])
def test_a_conversation_resumed_in_another_pane_while_the_restore_ran_isnt_started_again(
    path: Path, folder: Path, tmp_path: Path, by: str
) -> None:
    # It appeared after the panes were listed: resumed by hand, or by herdr, during the
    # seconds the restore runs. Listed again before the start, it's found and kept.
    shell = pane(agent=None, cwd=str(folder))
    late = pane("w5:p1", "t-late", session=UUID if by == "hook" else None, cwd=str(folder)) | {"agent_status": "working"}
    runner = runner_for([shell], **{"pane list": [ok({"panes": [shell]}), ok({"panes": [shell, late]})],
                                    "pane process-info --pane w5:p1": process_info(["claude", "--resume", UUID])})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("agent", "start") == []
    assert runner.called("pane", "close") == [["pane", "close", "w1:p1"]]
    assert "busy in w5:p1" in line and "closed the shell left in w1:p1" in line
    [kept] = load(path).agents
    assert (kept.pane_id, kept.terminal_id) == ("w5:p1", "t-late")


@pytest.mark.parametrize("by", ["hook", "command line"])
def test_a_conversation_resumed_in_a_pane_already_open_while_the_restore_ran_isnt_started_again(
    path: Path, folder: Path, tmp_path: Path, by: str
) -> None:
    # Both panes were shells when listed. While the first agent starts, the user resumes the
    # second's conversation in the shell herdr left at its old pane id.
    first = entry(folder, terminal_id="t-a", pane_id="w1:p1", session_id=OTHER_UUID)
    second = entry(folder, terminal_id="t-b", pane_id="w2:p1", session_id=UUID)
    shell_a = pane("w1:p1", "t-new1", agent=None, cwd=str(folder))
    shell_b = pane("w2:p1", "t-new2", agent=None, cwd=str(folder))
    now_b = pane("w2:p1", "t-new2", session=UUID if by == "hook" else None, cwd=str(folder)) | {
        "agent_status": "working"}
    runner = runner_for([shell_a, shell_b], **{
        "pane list": [ok({"panes": [shell_a, shell_b]}), ok({"panes": [shell_a, shell_b]}),
                      ok({"panes": [shell_a, now_b]})],
        "pane process-info --pane w2:p1": [shell_info(), process_info(["claude", "--resume", UUID])],
    })
    lines = run(runner, path, [first, second], tmp_path)

    assert len(runner.called("agent", "start")) == 1
    assert "busy in w2:p1" in lines[1]
    kept = load(path).agents[1]
    assert (kept.pane_id, kept.terminal_id) == ("w2:p1", "t-new2")


def test_herdrs_resume_with_other_args_whose_hook_session_shows_up_during_the_restore_is_replaced(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # Listed before herdr's hook gave its session, which shows up while the first agent
    # starts: what was read of its process still tells it runs without the saved arguments.
    first = entry(tmp_path / "other", terminal_id="t-a", pane_id="w1:p1", session_id=OTHER_UUID)
    second = entry(folder, terminal_id="t-b", pane_id="w2:p1", session_id=UUID)
    before = pane("w2:p1", "t-r", cwd=str(folder))
    after = pane("w2:p1", "t-r", session=UUID, cwd=str(folder))
    runner = runner_for([before], argv=["claude", "--resume", UUID],
                        **{"pane list": [ok({"panes": [before]}), ok({"panes": [after]})]})
    run(runner, path, [first, second], tmp_path)

    assert runner.called("pane", "close") == [["pane", "close", "w2:p1"]]
    assert len(runner.called("agent", "start")) == 2


def test_a_claude_pane_without_the_hook_resumed_by_hand_while_the_restore_ran_isnt_started_again(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # herdr lists it the same before and after: Claude Code without herdr's hook, quit and
    # started again with the second agent's conversation while the first agent started.
    first = entry(tmp_path / "other", terminal_id="t-a", pane_id="w1:p1", session_id=OTHER_UUID)
    second = entry(folder, terminal_id="t-b", pane_id="w2:p1", session_id=UUID)
    hand = pane("w7:p1", "t-hand", cwd=str(folder)) | {"agent_status": "working"}
    runner = runner_for([hand], **{
        "pane process-info --pane w7:p1": [process_info(["claude"]), process_info(["claude", "--resume", UUID])],
    })
    lines = run(runner, path, [first, second], tmp_path)

    assert len(runner.called("agent", "start")) == 1
    assert "busy in w7:p1" in lines[1]


def test_herdrs_resume_replaced_isnt_held_off_by_another_pane_in_its_folder_that_cant_be_read_again(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # The conversation was found and its pane closed to be replaced: another Claude pane in
    # the folder, read before as running none, failing a read now doesn't stop the start.
    resumed = pane("w2:p1", "t-r", session=UUID, cwd=str(folder))
    other = pane("w7:p1", "t-u", cwd=str(folder))
    runner = runner_for([resumed, other], **{
        "pane process-info --pane w2:p1": process_info(["claude", "--resume", UUID]),
        "pane process-info --pane w7:p1": [process_info(["claude"]), err("timeout"), process_info(["claude"])],
    })
    run(runner, path, [entry(folder, terminal_id="t-b", pane_id="w2:p1", session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == [["pane", "close", "w2:p1"]]
    assert len(runner.called("agent", "start")) == 1


def test_a_hand_resume_read_before_stays_its_conversations_when_it_cant_be_read_again(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # Read at the snapshot as running the second agent's conversation; every read after
    # fails. Still taken as running it: kept, not started a second time beside it.
    first = entry(tmp_path / "other", terminal_id="t-a", pane_id="w1:p1", session_id=OTHER_UUID)
    second = entry(folder, terminal_id="t-b", pane_id="w2:p1", session_id=UUID)
    hand = pane("w7:p1", "t-hand", cwd=str(tmp_path / "elsewhere"))
    runner = runner_for([hand], **{
        "pane process-info --pane w7:p1": [process_info(["claude", "--resume", UUID]), err("timeout")],
    })
    lines = run(runner, path, [first, second], tmp_path)

    assert len(runner.called("agent", "start")) == 1
    assert runner.called("pane", "close") == []
    assert "w7:p1 runs its conversation but its process couldn't be read" in lines[1]


def test_an_agent_pane_in_its_folder_with_no_foreground_processes_listed_holds_off_the_start(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # herdr listing none is as good as not answering: what runs in it can't be told.
    empty = ok({"process_info": {"foreground_process_group_id": 9000, "foreground_processes": [], "shell_pid": 9000}})
    runner = runner_for([pane("w7:p1", "t-u", cwd=str(folder))], **{"pane process-info --pane w7:p1": empty})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("agent", "start") == []
    assert "kept for the next restart: w7:p1" in line


THIRD_UUID = "33333333-2222-3333-4444-555555555555"


def test_a_conversation_the_hook_shows_in_a_pane_that_cant_be_read_again_isnt_started_again(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # Read as running a third conversation; while the first agent starts, the second's is
    # resumed in it (the hook says so), and reading it again fails once. What was read before
    # the change doesn't hide what the hook shows.
    first = entry(tmp_path / "other", terminal_id="t-a", pane_id="w1:p1", session_id=OTHER_UUID)
    second = entry(folder, terminal_id="t-b", pane_id="w2:p1", session_id=UUID)
    before = pane("w5:p1", "t-x", session=THIRD_UUID, cwd=str(folder))
    after = pane("w5:p1", "t-x", session=UUID, cwd=str(folder)) | {"agent_status": "working"}
    runner = runner_for([before], **{
        "pane list": [ok({"panes": [before]}), ok({"panes": [after]})],
        "pane process-info --pane w5:p1": [process_info(["claude", "--resume", THIRD_UUID]), err("timeout"),
                                           process_info(["claude", "--resume", UUID])],
    })
    run(runner, path, [first, second], tmp_path)

    assert len(runner.called("agent", "start")) == 1


def test_a_pane_that_left_the_conversation_isnt_taken_for_it_when_it_cant_be_read_again(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # herdr's resume of the second agent's conversation switches to another while the first
    # agent starts (the hook says so), and reading it again fails once: the agent is started.
    first = entry(tmp_path / "other", terminal_id="t-a", pane_id="w1:p1", session_id=OTHER_UUID)
    second = entry(folder, terminal_id="t-b", pane_id="w2:p1", session_id=UUID)
    before = pane("w2:p1", "t-x", session=UUID, cwd=str(folder))
    after = pane("w2:p1", "t-x", session=THIRD_UUID, cwd=str(folder))
    runner = runner_for([before], **{
        "pane list": [ok({"panes": [before]}), ok({"panes": [after]})],
        "pane process-info --pane w2:p1": [process_info(["claude", "--resume", UUID]), err("timeout"),
                                           process_info(["claude", "--resume", THIRD_UUID])],
    })
    run(runner, path, [first, second], tmp_path)

    assert len(runner.called("agent", "start")) == 2


def test_a_claude_pane_without_the_hook_in_its_folder_that_cant_be_read_again_holds_off_the_start(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # Read as running a conversation no saved agent has; it may have been started again by
    # hand with the agent's since, which herdr's listing doesn't show without the hook.
    hand = pane("w5:p1", "t-p", cwd=str(folder))
    runner = runner_for([hand], **{
        "pane process-info --pane w5:p1": [process_info(["claude", "--resume", THIRD_UUID]), err("timeout"),
                                           process_info(["claude", "--resume", UUID])],
    })
    [line] = run(runner, path, [entry(folder, terminal_id="t-b", pane_id="w2:p1", session_id=UUID)], tmp_path)

    assert runner.called("agent", "start") == []
    assert "kept for the next restart: w5:p1" in line


def test_a_conversation_resumed_in_a_shell_that_couldnt_be_closed_isnt_started_again(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # The first agent's leftover shell couldn't be closed; the second's conversation is then
    # resumed in it by hand, without the hook.
    other = tmp_path / "other"
    first = entry(other, terminal_id="t-a", pane_id="w1:p1", session_id=OTHER_UUID)
    second = entry(folder, terminal_id="t-b", pane_id="w2:p1", session_id=UUID)
    shell = pane("w1:p1", "t-s", agent=None, cwd=str(other))
    hand = pane("w1:p1", "t-s", cwd=str(other)) | {"agent_status": "working"}
    runner = runner_for([shell], **{
        "pane list": [ok({"panes": [shell]}), ok({"panes": [shell]}), ok({"panes": [hand]})],
        "pane close": err("timeout"),
        "pane process-info --pane w1:p1": [shell_info(), process_info(["claude", "--resume", UUID])],
    })
    lines = run(runner, path, [first, second], tmp_path)

    assert len(runner.called("agent", "start")) == 1
    assert "busy in w1:p1" in lines[1]


def test_herdrs_resume_whose_claude_exited_before_its_process_was_read_is_closed_and_replaced(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # herdr still listed it as a Claude agent, but its processes, read, show only the shell:
    # not one that couldn't be read, so not kept, which would age the agent out unrestored.
    resumed = pane("w2:p1", "t-r", session=UUID, cwd=str(folder))
    runner = runner_for([resumed], **{"pane process-info --pane w2:p1": shell_info(),
                                      "agent get w2:p1": ok({"agent": resumed | {"agent": None}})})
    [line] = run(runner, path, [entry(folder, terminal_id="t-b", pane_id="w2:p1", session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == [["pane", "close", "w2:p1"]]
    assert len(runner.called("agent", "start")) == 1
    assert "restored in" in line


def test_a_busy_pane_running_its_conversation_with_claude_suspended_isnt_closed(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # Claude Code stopped with ctrl-Z: only the shell is in the foreground, but herdr calls it
    # busy, which may be mid-turn.
    resumed = pane("w2:p1", "t-r", session=UUID, cwd=str(folder)) | {"agent_status": "working"}
    runner = runner_for([resumed], **{"pane process-info --pane w2:p1": shell_info()})
    [line] = run(runner, path, [entry(folder, terminal_id="t-b", pane_id="w2:p1", session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == runner.called("agent", "start") == []
    assert "busy in w2:p1" in line


def test_an_idle_pane_running_its_conversation_with_claude_among_its_jobs_isnt_closed(
    path: Path, folder: Path, tmp_path: Path, jobs: set[int]
) -> None:
    # Suspended, so not in the foreground, but still under the shell: not exited.
    jobs.add(9000)
    resumed = pane("w2:p1", "t-r", session=UUID, cwd=str(folder))
    runner = runner_for([resumed], **{"pane process-info --pane w2:p1": shell_info(),
                                      "agent get w2:p1": ok({"agent": resumed})})
    run(runner, path, [entry(folder, terminal_id="t-b", pane_id="w2:p1", session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == runner.called("agent", "start") == []


@pytest.mark.parametrize("status", [err("timeout"), "working"])
def test_herdrs_resume_that_exited_whose_status_isnt_idle_now_keeps_the_agent_for_the_next_restart(
    path: Path, folder: Path, tmp_path: Path, status
) -> None:
    # Only the shell runs in it, but herdr can't say, or still calls it busy: left, and the
    # agent kept rather than tied to a pane no save lists, where it would age out.
    resumed = pane("w2:p1", "t-r", session=UUID, cwd=str(folder))
    answer = status if isinstance(status, tuple) else ok({"agent": resumed | {"agent_status": status}})
    runner = runner_for([resumed], **{"pane process-info --pane w2:p1": shell_info(), "agent get w2:p1": answer})
    [line] = run(runner, path, [entry(folder, terminal_id="t-b", pane_id="w7:p1", session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == runner.called("agent", "start") == []
    assert "kept for the next restart" in line
    restore_module._write(path, [], False)
    restore_module._write(path, [], False)
    [kept] = load(path).agents
    assert kept.session_id == UUID and kept.restore_failed


def test_a_pane_whose_claude_was_suspended_since_it_was_read_isnt_closed_as_a_shell(
    path: Path, folder: Path, tmp_path: Path, jobs: set[int]
) -> None:
    # Read running the conversation with other arguments; then stopped with ctrl-Z, so herdr
    # shows no agent and only the shell is in the foreground. Not idle: it still runs.
    jobs.add(9000)
    resumed = pane("w2:p1", "t-r", session=UUID, cwd=str(folder))
    runner = runner_for([resumed], **{
        "pane process-info --pane w2:p1": [process_info(["claude", "--resume", UUID]), shell_info()],
        "agent get w2:p1": ok({"agent": resumed | {"agent": None}}),
    })
    run(runner, path, [entry(folder, terminal_id="t-b", pane_id="w7:p1", session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == runner.called("agent", "start") == []


@pytest.mark.parametrize("read_before", [False, True])
def test_herdrs_resume_that_exited_doesnt_lift_the_hold_off_for_a_pane_in_its_folder_that_cant_be_read(
    path: Path, folder: Path, tmp_path: Path, read_before: bool
) -> None:
    # It never ran the conversation: a pane opened meanwhile in the folder, which can't be
    # read, may, so the start is held off.
    resumed = pane("w2:p1", "t-r", session=UUID, cwd=str(folder))
    hand = pane("w3:p1", "t-u", cwd=str(folder)) | {"agent_status": "working"}
    reads = [process_info(["claude", "--resume", UUID]), shell_info()] if read_before else shell_info()
    runner = runner_for([resumed], **{
        "pane list": [ok({"panes": [resumed]}), ok({"panes": [resumed, hand]})],
        "pane process-info --pane w2:p1": reads,
        "agent get w2:p1": ok({"agent": resumed | {"agent": None}}),
        "pane process-info --pane w3:p1": err("timeout"),
    })
    [line] = run(runner, path, [entry(folder, terminal_id="t-b", pane_id="w7:p1", session_id=UUID)], tmp_path)

    assert runner.called("agent", "start") == []
    assert "kept for the next restart: w3:p1" in line


def test_a_claude_agent_opened_in_its_folder_while_the_restore_ran_that_cant_be_read_holds_off_the_start(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # As for one listed at the start: it may run the conversation, so the start waits.
    late = pane("w5:p1", "t-late", cwd=str(folder))
    runner = runner_for([], **{"pane list": [ok({"panes": []}), ok({"panes": [late]})],
                               "pane process-info --pane w5:p1": err("timeout")})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("agent", "start") == []
    assert runner.called("pane", "close") == []
    assert "kept for the next restart: w5:p1" in line
    [kept] = load(path).agents
    assert kept.restore_failed


def test_a_claude_agent_opened_elsewhere_while_the_restore_ran_that_cant_be_read_doesnt_stop_the_start(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    late = pane("w5:p1", "t-late", cwd=str(tmp_path))
    runner = runner_for([], **{"pane list": [ok({"panes": []}), ok({"panes": [late]})],
                               "pane process-info --pane w5:p1": err("timeout")})
    run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert len(runner.called("agent", "start")) == 1


def test_a_conversation_resumed_idle_in_another_pane_while_the_restore_ran_with_other_args_is_replaced(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    late = pane("w5:p1", "t-late", session=UUID, cwd=str(folder))
    runner = runner_for([], **{"pane list": [ok({"panes": []}), ok({"panes": [late]})],
                               "pane process-info --pane w5:p1": process_info(["claude", "--resume", UUID]),
                               "agent get w5:p1": ok({"agent": late})})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == [["pane", "close", "w5:p1"]]
    assert claude_args(runner) == ["--resume", UUID, "--permission-mode", "auto"]
    assert "closed herdr's own resume in w5:p1" in line


def test_a_failed_list_before_the_start_doesnt_stop_it(path: Path, folder: Path, tmp_path: Path) -> None:
    runner = runner_for([], **{"pane list": [ok({"panes": []}), err("timeout")]})
    run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert len(runner.called("agent", "start")) == 1
    assert len(runner.called("pane", "list")) == 2


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


def test_a_pane_kept_for_one_entry_isnt_closed_for_another_when_the_file_cant_be_written(
    path: Path, folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # herdr resumed the first one as it was; the second saved the same conversation with
    # other arguments, so it would take that pane for a resume to replace.
    def unwritable(*args: object) -> None:
        raise StateError(f"Can't write {path} (No space left on device).")

    monkeypatch.setattr(restore_module, "_replace", unwritable)
    runner = runner_for([pane(session=UUID)], ["claude", "--permission-mode", "auto", "--resume", UUID])
    first = entry(folder, session_id=UUID)
    second = entry(tmp_path / "api", terminal_id="t-old-2", pane_id="w2:p1", session_id=UUID, args=[])
    lines = run(runner, path, [first, second], tmp_path)

    assert runner.called("pane", "close") == runner.called("agent", "start") == []
    assert "already runs" in lines[1]


def test_a_restored_agent_whose_entry_couldnt_be_written_drops_its_stale_entry(
    path: Path, folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Its stale entry keeps the old pane id; were it kept, a pane still open there would make
    # every later round look like a herdr restart and start the agent again.
    def unwritable(*args: object) -> None:
        raise StateError(f"Can't write {path} (No space left on device).")

    monkeypatch.setattr(restore_module, "_replace", unwritable)
    save(path, SavedAgents(agents=[entry(folder, session_id=UUID)]))
    herdr = Herdr(runner=runner_for([]))
    service = AgentService(Settings(token=TOKEN, allowed_roots=(tmp_path.resolve(),)), herdr)

    [line] = asyncio.run(restore(service, herdr, path))

    assert "restored in w90:p1" in line and "No space left on device" in line
    # Gone for good: the watcher's next write drops it and saves the new agent instead.
    assert service.stopped == {"t-old"}
    restore_module._write(path, [], True, service.stopped)
    assert load(path).agents == []


def test_an_adopted_agent_whose_entry_couldnt_be_written_drops_its_stale_entry(
    path: Path, folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As for a restored one: the stale entry's old pane id, now another terminal's, would
    # make the next round look like a herdr restart.
    def unwritable(*args: object) -> None:
        raise StateError(f"Can't write {path} (No space left on device).")

    monkeypatch.setattr(restore_module, "_replace", unwritable)
    busy = pane(session=UUID, cwd=str(folder)) | {"agent_status": "working"}
    save(path, SavedAgents(agents=[entry(folder, session_id=UUID)]))
    herdr = Herdr(runner=runner_for([busy], ["claude", "--resume", UUID]))
    service = AgentService(Settings(token=TOKEN, allowed_roots=(tmp_path.resolve(),)), herdr)

    [line] = asyncio.run(restore(service, herdr, path))

    assert "No space left on device" in line
    assert service.stopped == {"t-old"}


def test_agents_json_is_read_under_its_lock_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path, tmp_path: Path
) -> None:
    # On Windows a read while a forget or save-agents replaces the file fails; and the lock
    # may be waited for, which mustn't hold up the event loop.
    held: list[bool] = []
    reads: list[tuple[bool, bool]] = []
    real_locked, real_load = restore_module.locked, restore_module.load

    @contextlib.contextmanager
    def recording(locked_path: Path):
        with real_locked(locked_path):
            held.append(True)
            try:
                yield
            finally:
                held.pop()

    def checked(read_path: Path, **kwargs: bool):
        reads.append((bool(held), threading.current_thread() is threading.main_thread()))
        return real_load(read_path, **kwargs)

    monkeypatch.setattr(restore_module, "locked", recording)
    monkeypatch.setattr(restore_module, "load", checked)
    run(runner_for([]), path, [entry(folder, session_id=UUID)], tmp_path)
    asyncio.run(restore_module._restarted(Herdr(runner=runner_for([])), path, True))

    # (under the lock, on the event loop's thread), for each read.
    assert reads and reads == [(True, False)] * len(reads)


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


def test_an_agent_stopped_while_its_restore_starts_it_doesnt_come_back(
    path: Path, folder: Path, tmp_path: Path
) -> None:
    # The new pane shows up while herdr still waits for Claude Code in it, and the user stops it.
    new = started(0) | {"cwd": str(folder)}
    fake = runner_for([], **{"agent get": ok({"agent": new})})
    start_reached, release = asyncio.Event(), asyncio.Event()
    closed: list[str] = []

    async def runner(argv) -> tuple[int, str, str]:
        args = list(argv[1:])
        if args[:2] == ["agent", "start"]:
            start_reached.set()
            await release.wait()
            if args[args.index("--pane") + 1] in closed:
                return err("pane_not_found")
        if args[:2] == ["pane", "close"]:
            closed.append(args[2])
        return await fake(argv)

    save(path, SavedAgents(agents=[entry(folder, session_id=UUID)]))
    herdr = Herdr(runner=runner)
    service = AgentService(Settings(token=TOKEN, allowed_roots=(tmp_path.resolve(),)), herdr, saved_path=path)

    async def stop_meanwhile() -> None:
        await start_reached.wait()
        stopping = asyncio.create_task(service.stop_agent("w90:p1"))
        # Long enough for a stop that doesn't wait for the start to close the pane.
        await asyncio.sleep(0.1)
        release.set()
        await stopping

    async def both() -> list[str]:
        lines, _ = await asyncio.gather(restore(service, herdr, path), stop_meanwhile())
        return lines

    [line] = asyncio.run(both())

    assert "restored in w90:p1" in line
    assert closed == ["w90:p1"]
    # The watcher's next write leaves nothing that a restart would bring back.
    restore_module._write(path, [], True, service.stopped)
    assert load(path).agents == []


@pytest.mark.parametrize("stopped", ["t-old", "t-new"])
def test_an_agent_stopped_through_the_bridge_is_never_restored(
    path: Path, folder: Path, tmp_path: Path, stopped: str
) -> None:
    # Stopped, but its forget couldn't write agents.json (t-old); or the pane resuming its
    # conversation was stopped since the restore listed the panes (t-new). Otherwise its
    # pane would be closed and the agent started again.
    runner = runner_for([pane(session=UUID, cwd=str(folder))], ["claude", "--resume", UUID])
    save(path, SavedAgents(agents=[entry(folder, session_id=UUID)]))
    herdr = Herdr(runner=runner)
    service = AgentService(Settings(token=TOKEN, allowed_roots=(tmp_path.resolve(),)), herdr)
    service.stopped.add(stopped)

    [line] = asyncio.run(restore(service, herdr, path))

    assert runner.called("agent", "start") == runner.called("pane", "close") == []
    assert "stopped" in line
    assert load(path).agents == []


@pytest.mark.parametrize("during", ["agent get", "pane close"])
def test_herdrs_resume_stopped_through_the_bridge_as_the_restore_closes_it_doesnt_come_back(
    path: Path, folder: Path, tmp_path: Path, during: str
) -> None:
    # stop_agent closed it just before the restore read its status again or closed it, so
    # that failed and the pane is gone: its conversation isn't started again.
    resumed = pane(session=UUID, cwd=str(folder))
    fake = runner_for([resumed], ["claude", "--resume", UUID],
                      **{"pane list": [ok({"panes": [resumed]}), ok({"panes": []})]})
    herdr = Herdr(runner=None)
    service = AgentService(Settings(token=TOKEN, allowed_roots=(tmp_path.resolve(),)), herdr)

    async def runner(argv) -> tuple[int, str, str]:
        if " ".join(argv[1:]).startswith(during):
            fake.calls.append(list(argv[1:]))
            service.stopped.add("t-new")
            return err("pane_not_found" if during == "pane close" else "agent_not_found")
        return await fake(argv)

    herdr.runner = runner
    save(path, SavedAgents(agents=[entry(folder, session_id=UUID)]))
    [line] = asyncio.run(restore(service, herdr, path))

    assert fake.called("agent", "start") == []
    assert "stopped" in line
    assert load(path).agents == []


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


def test_a_failed_entrys_old_pane_id_isnt_taken_for_its_shell(path: Path, folder: Path, tmp_path: Path) -> None:
    # Its leftover was dealt with at the restore that failed; its pane id is stale by design,
    # so an idle shell there now is someone else's.
    runner = runner_for([pane(agent=None, cwd=str(folder))])
    run(runner, path, [entry(folder, session_id=UUID, restore_failed=True)], tmp_path)

    assert runner.called("pane", "close") == []
    assert len(runner.called("agent", "start")) == 1


def test_a_shell_pane_running_a_program_is_never_closed(path: Path, folder: Path, tmp_path: Path) -> None:
    # Same id and folder, but the user runs something in it (an editor, a dev server).
    runner = runner_for([pane(agent=None, cwd=str(folder))], **{"pane process-info --pane w1:p1": shell_info("vim")})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == []
    assert len(runner.called("agent", "start")) == 1
    assert "closed" not in line


def test_a_shell_pane_with_no_foreground_processes_listed_isnt_closed(path: Path, folder: Path, tmp_path: Path) -> None:
    # herdr gave none: what runs in it isn't known, so it isn't known to be idle.
    empty = ok({"process_info": {"foreground_process_group_id": 9000, "foreground_processes": [], "shell_pid": 9000}})
    runner = runner_for([pane(agent=None, cwd=str(folder))], **{"pane process-info --pane w1:p1": empty})
    [line] = run(runner, path, [entry(folder, session_id=UUID)], tmp_path)

    assert runner.called("pane", "close") == []
    assert len(runner.called("agent", "start")) == 1
    assert "closed" not in line


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
    down = HerdrError("server_not_running", "herdr is down")
    # Rounds 1 and 2 fail; round 3 restores (two snapshots); round 4 finds t1 among the panes.
    rounds = Rounds(monkeypatch, path, [down, down, [], [seen], [seen]], [["t1"]])
    rounds.watch(4)
    assert rounds.restores == [3]
    # Nothing is written while herdr doesn't answer.
    assert rounds.files[:2] == [None, None]
    assert rounds.files[2:] == [[seen], [seen]]


def test_all_terminal_ids_new_counts_as_restart(monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path) -> None:
    first, second = entry(folder, terminal_id="t-a", pane_id="w1:p1"), entry(folder, terminal_id="t-b", pane_id="w2:p1")
    save(path, SavedAgents(agents=[first, second]))
    # herdr's first answer: its panes have new terminals, and ids no saved agent had, so the
    # pane-id rule can't tell; that no saved terminal is live does.
    new = [entry(folder, terminal_id="t-a2", pane_id="w5:p1"), entry(folder, terminal_id="t-b2", pane_id="w6:p1")]
    rounds = Rounds(monkeypatch, path, [[], new], [[("w5:p1", "t-a2"), ("w6:p1", "t-b2")]])
    rounds.watch(1)
    assert rounds.restores == [1]


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
    down = HerdrError("server_not_running", "herdr is down")
    # Round 3 answers after an outage; t1 is still there, in a pane elsewhere.
    rounds = Rounds(monkeypatch, path, [[seen], [seen], down, [seen]], [[("w2:p1", "t1")]])
    rounds.watch(3)
    assert rounds.restores == [1]


def test_answering_again_with_no_saved_terminal_live_is_a_restart(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path
) -> None:
    old, new = entry(folder, terminal_id="t-old"), entry(folder, terminal_id="t-new")
    down = HerdrError("server_not_running", "herdr is down")
    rounds = Rounds(monkeypatch, path, [[old], [old], down, [new], [new]], [[("w2:p1", "t-new")]])
    rounds.watch(3)
    assert rounds.restores == [1, 3]


@pytest.mark.parametrize("code", ["timeout", "bad_output", "cli_failed"])
def test_a_passing_herdr_error_doesnt_make_the_next_round_look_like_a_new_server(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path, code: str
) -> None:
    # herdr's server ran on: round 2 failed for another reason, and before round 3 the user
    # closed the only agent by hand, which no restore may bring back.
    seen = entry(folder, terminal_id="t1")
    rounds = Rounds(monkeypatch, path, [[seen], [seen], HerdrError(code, "passing"), []], [[]])
    rounds.watch(3)
    assert rounds.restores == [1]


def test_a_failed_restart_check_on_herdrs_first_round_is_made_again_next_round(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path
) -> None:
    # The host rebooted and herdr came back without its layout. Its first answer lists the
    # agents, but looking at its panes then fails once: the next round is still herdr's
    # first, or the saved agents would age out unrestored.
    save(path, SavedAgents(agents=[entry(folder, terminal_id="t-old", pane_id="w5:p1")]))
    rounds = Rounds(monkeypatch, path, [[], [], []], [])
    answers: list = [HerdrError("timeout", "slow"), [], []]

    async def list_panes(_herdr: object = None) -> list[dict]:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    rounds.list_panes = list_panes
    rounds.watch(3)
    assert rounds.restores == [2]


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
    # Listing herdr's panes failed: unlike an entry's own failure, that ends the whole run.
    rounds.restore_errors = [HerdrError("timeout", "herdr pane list timed out")]
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
            if not self.panes.get(args[-1], {}).get("agent"):
                return shell_info()
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


def unreadable_once(monkeypatch: pytest.MonkeyPatch, path: Path, read: int) -> None:
    """The `read`th read of `path` fails as a Windows sharing violation would."""
    real_read = Path.read_bytes
    reads = [0]

    def flaky(self: Path) -> bytes:
        if self == path:
            reads[0] += 1
            if reads[0] == read:
                raise PermissionError(13, "Access is denied")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)


def test_a_restore_that_couldnt_read_agents_json_runs_again_next_round(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path, tmp_path: Path
) -> None:
    # herdr came back without its layout. The restart check reads the file; the restore's own
    # read then fails once. Read as no saved agents, the restore would end normally and the
    # agent age out unrestored.
    unreadable_once(monkeypatch, path, 2)
    restores = watch_host(monkeypatch, Host([]), path, [entry(folder, session_id=UUID)], tmp_path, 2)

    assert restores == [1, 2]
    [restored] = load(path).agents
    assert (restored.pane_id, restored.session_id) == ("w91:p1", UUID)


def lock_held_for_forget(monkeypatch: pytest.MonkeyPatch) -> None:
    """The restore's first forget times out on agents.json's lock."""
    real_forget = restore_module.forget
    calls = [0]

    def flaky(forget_path: Path, terminal_id: str) -> None:
        calls[0] += 1
        if calls[0] == 1:
            raise TimeoutError("agents.json.lock is held by another process.")
        real_forget(forget_path, terminal_id)

    monkeypatch.setattr(restore_module, "forget", flaky)


def test_an_entry_whose_forget_failed_doesnt_set_off_another_restore(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path, tmp_path: Path
) -> None:
    # The new server numbered panes afresh: w1:p1 is now the user's shell elsewhere. The
    # entry that had already gone keeps that pane id when its forget fails; in the next
    # round it would look like a restart again, and bring back the agent the user closed.
    lock_held_for_forget(monkeypatch)
    restored = entry(folder, name="restored", terminal_id="t-a", pane_id="w2:p1")
    gone = entry(tmp_path / "api", name="gone", terminal_id="t-g", pane_id="w1:p1", misses=1)
    host = Host([pane("w1:p1", terminal_id="t-x", agent=None, cwd=str(tmp_path / "other"))])
    restores = watch_host(monkeypatch, host, path, [restored, gone], tmp_path, 2,
                          {1: lambda: host.close("restored")})

    assert restores == [1]
    assert host.started == ["restored"]


@pytest.mark.parametrize("case", ["gone before", "conversation runs", "untrusted", "resume stopped"])
def test_an_entry_whose_forget_failed_is_dropped_by_the_next_write(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path, tmp_path: Path, case: str
) -> None:
    lock_held_for_forget(monkeypatch)
    runner = runner_for([])
    saved = [entry(folder, session_id=UUID)]
    stopped: set[str] = set()
    if case == "gone before":
        saved = [entry(folder, misses=1)]
    elif case == "conversation runs":
        survivor = entry(folder, terminal_id="t-live", pane_id="w2:p1", session_id=UUID)
        runner = runner_for([pane("w2:p1", terminal_id="t-live", session=UUID)], ["claude", "--resume", UUID])
        saved = [saved[0], survivor]
    elif case == "untrusted":
        runner = runner_for([], **{"pane wait-output": ok({"matched_line": "Yes, I trust this folder"})})
    else:
        runner = runner_for([pane(session=UUID, cwd=str(folder))], ["claude", "--resume", UUID])
        stopped.add("t-new")
    save(path, SavedAgents(agents=saved))
    herdr = Herdr(runner=runner)
    service = AgentService(Settings(token=TOKEN, allowed_roots=(tmp_path.resolve(),)), herdr)
    service.stopped |= stopped

    lines = asyncio.run(restore(service, herdr, path))

    assert "is held by another process" in lines[0]
    assert "t-old" in service.stopped
    restore_module._write(path, [], True, service.stopped)
    assert "t-old" not in [agent.terminal_id for agent in load(path).agents]


def test_a_restart_check_that_couldnt_read_agents_json_fails(
    monkeypatch: pytest.MonkeyPatch, path: Path, folder: Path
) -> None:
    # Read as no saved agents, it would say "no restart" for a server that brought the
    # agent's pane back with a new terminal; failing, it's made again next round.
    save(path, SavedAgents(agents=[entry(folder)]))
    herdr = Herdr(runner=runner_for([pane(terminal_id="t-other")]))
    unreadable_once(monkeypatch, path, 1)

    with pytest.raises(StateError):
        asyncio.run(restore_module._restarted(herdr, path, True))


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
