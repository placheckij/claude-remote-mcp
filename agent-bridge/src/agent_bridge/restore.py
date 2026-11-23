"""Bring back the Claude Code agents saved in `agents.json` after the host or herdr's server
restarts: each in a workspace of its own, on its account, with its arguments and, when it
can, its conversation. A restored agent is never sent a prompt.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from agent_bridge.agents import FINISHED, AgentService
from agent_bridge.claude_args import session_id_in
from agent_bridge.claude_process import claude_process
from agent_bridge.errors import BridgeError
from agent_bridge.herdr import SERVER_NOT_RUNNING, Herdr, HerdrError
from agent_bridge.saved_agents import SavedAgent, forget, load, locked, merge, save, snapshot
from agent_bridge.state import StateError

log = logging.getLogger(__name__)


@dataclass
class _Found:
    """What one restore found in herdr, and what it has done so far."""

    panes: list[dict]
    # What runs in each Claude pane, to tell whether herdr resumed a conversation as it was.
    live: dict[str, SavedAgent]
    # Terminals of the saved agents and of those restored so far: a pane of theirs runs that
    # agent, never herdr's resume of another.
    claimed: set[str]
    # Conversations a saved agent still runs, or one restored so far does: each runs once.
    sessions: set[str]
    closed: set[str] = field(default_factory=set)
    # The foreground processes of panes herdr doesn't know as agents, by pane id, as read
    # once; {} when they couldn't be.
    processes: dict[str, dict] = field(default_factory=dict)


async def restore(service: AgentService, herdr: Herdr, path: Path) -> list[str]:
    """Start each saved agent that isn't running, one after another in the file's order.
    Each outcome is logged; the lines are returned too. A HerdrError from listing herdr's
    panes or agents propagates: herdr isn't up."""
    entries = await asyncio.to_thread(_saved, path)
    panes = await herdr.list_panes()
    terminals = {pane.get("terminal_id") for pane in panes}
    found = _Found(
        panes=panes,
        live={agent.pane_id: agent for agent in await snapshot(herdr)},
        claimed={entry.terminal_id for entry in entries},
        sessions={entry.session_id for entry in entries if entry.session_id and entry.terminal_id in terminals},
    )
    lines = []
    for entry in entries:
        try:
            line, failed = await _restore_one(service, herdr, path, entry, entries, found)
        except (OSError, StateError) as error:
            # agents.json couldn't be read or written (a full disk, a held lock). The run
            # still goes on and ends, so the watcher doesn't start this agent again.
            line, failed = f"{entry.name or entry.cwd}: agents.json not updated: {error}", True
        (log.warning if failed else log.info)("%s", line)
        lines.append(line)
    return lines


async def _restore_one(
    service: AgentService, herdr: Herdr, path: Path, entry: SavedAgent, entries: list[SavedAgent], found: _Found
) -> tuple[str, bool]:
    """The log line for `entry`, and whether it says something failed."""
    label = entry.name or entry.cwd
    if entry.terminal_id in service.stopped:
        # Stopped through the bridge, whose forget couldn't take it out of agents.json.
        await _forget(path, entry, service.stopped)
        return f"{label}: not restored, and forgotten: it was stopped through the bridge", False
    for pane in found.panes:
        if pane.get("terminal_id") == entry.terminal_id:
            return f"{label}: still running in {pane['pane_id']}, left as it is", False
    open_panes = [pane for pane in found.panes if pane["pane_id"] not in found.closed]
    # herdr brings an agent's pane back under its pane id, in its folder. The folder matters
    # too: a herdr server that lost its session numbers panes afresh, and a pane of the
    # user's elsewhere can have the saved id.
    # Not for an entry whose restore failed: its leftover was dealt with then, and it keeps
    # its old pane id on purpose, so a pane there now is someone else's.
    back = [] if entry.restore_failed else [
        pane for pane in found.panes
        if pane["pane_id"] == entry.pane_id and pane.get("cwd") and Path(pane["cwd"]) == Path(entry.cwd)
    ]
    shell = next((pane for pane in back if pane["pane_id"] not in found.closed and not pane.get("agent")), None)
    if shell is not None and not await _idle_shell(herdr, shell):
        # Something runs in it (an editor, a dev server): the user's, not a leftover.
        shell = None
    if entry.session_id in found.sessions:
        # Two panes ran one conversation; a second copy of it would answer as the first.
        _, note = await _close(herdr, shell, found)
        await _forget(path, entry, service.stopped)
        return f"{label}: not restored, and forgotten: its conversation {entry.session_id} already runs{note}", False
    if entry.misses > 0 and not back:
        # It had already disappeared while this herdr server ran, and herdr didn't bring its
        # pane back: stopped or closed, not lost. Forgotten now rather than left to age out,
        # so its stale pane id can't match a pane a later server hands out.
        await _forget(path, entry, service.stopped)
        return f"{label}: not restored, and forgotten: it had already gone before the restart", False
    resumed = next(
        (
            # A restored shell still carries the session id it last ran: only a pane with an
            # agent in it is herdr's own resume.
            pane for pane in open_panes
            if entry.session_id and pane.get("agent") and pane.get("terminal_id") not in found.claimed
            and _session_in(pane, found) == entry.session_id
        ),
        None,
    )
    if resumed is None and entry.session_id:
        resumed = await _unclassified(herdr, entry.session_id, open_panes, found)
    if resumed is not None and resumed.get("terminal_id") in service.stopped:
        # Stopped through the bridge since the panes were listed.
        await _forget(path, entry, service.stopped)
        return (f"{label}: not restored, and forgotten: {resumed['pane_id']}, which ran its conversation, "
                "was stopped through the bridge"), False
    if resumed is not None:
        running = found.live.get(resumed["pane_id"])
        # By the config each starts on (for a saved agent whose account couldn't be read, its
        # folder's). The running one's own account is never guessed.
        as_it_was = running is not None and not running.config_dir_unknown and running.args == entry.args and (
            service.config_dir_of(running) == service.config_dir_of(entry)
        )
        # Busy: someone resumed it and is in the middle of a turn (working, or blocked on an
        # approval), likely by hand, since setup turns herdr's own resume off. Closing it
        # would end that turn. Whatever herdr doesn't know to be idle counts: "unknown" may
        # be mid-turn too.
        busy = resumed.get("agent_status") not in FINISHED
        # Its process couldn't be read, so whether it runs as it was can't be told: left, as
        # a shell that can't be read is. So is one with the agent's arguments whose
        # environment, so its account, couldn't be read.
        unreadable = running is None or (running.config_dir_unknown and running.args == entry.args)
        if as_it_was or busy or unreadable:
            await _adopt(path, entry, resumed, found, service.stopped)
            if as_it_was:
                return f"{label}: herdr resumed it in {resumed['pane_id']} as it was, left as it is", False
            if not resumed.get("agent"):
                return (f"{label}: {resumed['pane_id']} runs its conversation, though herdr doesn't know it "
                        "as an agent yet, left as it is"), False
            if busy:
                return f"{label}: busy in {resumed['pane_id']}, which runs its conversation, left as it is", False
            what = "process" if running is None else "account"
            return (f"{label}: {resumed['pane_id']} runs its conversation but its {what} couldn't be read, "
                    "left as it is"), False
        # herdr's own resume runs plain `claude --resume <id>`: without the account's
        # CLAUDE_CONFIG_DIR or the agent's arguments.
        closed, note = await _close(herdr, resumed, found, "herdr's own resume")
        if not closed:
            # Still running its conversation: a second copy would answer as the first.
            await _adopt(path, entry, resumed, found, service.stopped)
            return (f"{label}: {resumed['pane_id']} runs its conversation but couldn't be closed{note}, "
                    "left as it is"), True
    else:
        _, note = await _close(herdr, shell, found)

    if entry.session_id and not await asyncio.to_thread(
        _has_conversation, service.config_dir_of(entry), entry.session_id
    ):
        # Never prompted: Claude Code writes no conversation until the first prompt, and
        # refuses to --resume one it doesn't have. The same id keeps the entry's identity.
        resume = ["--session-id", entry.session_id]
        how = f"starting fresh as {entry.session_id}: it had no conversation yet"
    elif entry.session_id:
        resume, how = ["--resume", entry.session_id], f"resuming {entry.session_id}"
    elif not any(
        # By the config each starts on, which for an unknown one is its folder's account.
        other is not entry
        and Path(other.cwd) == Path(entry.cwd)
        and service.config_dir_of(other) == service.config_dir_of(entry)
        for other in entries
    ):
        resume, how = ["--continue"], "continuing its folder's last conversation"
    else:
        # --continue would pick the same conversation for both of them.
        resume, how = [], f"fresh conversation: another saved agent shares {entry.cwd} and its account"

    try:
        agent = await service.restart_agent(entry, resume)
    except BridgeError as error:
        if error.code == "folder_not_trusted":
            await _forget(path, entry, service.stopped)
            return f"{label}: not restored, and forgotten: {error.message}{note}", True
        await _keep_failed(path, entry)
        return f"{label}: not restored ({error.code}: {error.message}), kept for the next restart{note}", True
    except Exception as error:  # noqa: BLE001 - one agent's failure mustn't stop the others
        # Any other failure is likely passing (herdr busy, a slow start): try again next time.
        await _keep_failed(path, entry)
        return f"{label}: not restored ({error}), kept for the next restart{note}", True

    found.claimed.add(agent["terminal_id"])
    if entry.session_id:
        found.sessions.add(entry.session_id)
    restarted = entry.model_copy(
        update={"pane_id": agent["pane_id"], "terminal_id": agent["terminal_id"], "misses": 0,
                "restore_failed": False}
    )
    try:
        await asyncio.to_thread(_replace, path, entry.terminal_id, restarted)
    except (OSError, StateError) as error:
        # The old terminal is gone for good, as a stopped agent's is: the watcher's next write
        # drops the stale entry, whose old pane id could otherwise make later rounds look like
        # a herdr restart, and saves the new agent from its snapshot.
        service.stopped.add(entry.terminal_id)
        return f"{label}: restored in {agent['pane_id']}, {how}{note}, but agents.json not updated: {error}", True
    return f"{label}: restored in {agent['pane_id']}, {how}{note}", False


async def _idle_shell(herdr: Herdr, pane: dict) -> bool:
    """Whether the pane's shell runs nothing: its only foreground process is the shell itself.
    False when herdr can't say, so an unknown pane is left alone."""
    try:
        info = await herdr.process_info(pane["pane_id"])
    except HerdrError:
        return False
    shell = info.get("shell_pid")
    processes = info.get("foreground_processes") or []
    return shell is not None and all(isinstance(p, dict) and p.get("pid") == shell for p in processes)


async def _unclassified(herdr: Herdr, session_id: str, panes: list[dict], found: _Found) -> dict | None:
    """A pane herdr doesn't know as an agent yet (Claude Code in it has only just started)
    whose Claude Code's command line runs `session_id`. Not one herdr calls idle or done, nor
    one whose processes can't be read. It has no status of its own, so it's taken as busy."""
    for pane in panes:
        if pane.get("agent") or pane.get("agent_status") in FINISHED or pane.get("terminal_id") in found.claimed:
            continue
        pane_id = pane["pane_id"]
        if pane_id not in found.processes:
            try:
                found.processes[pane_id] = await herdr.process_info(pane_id)
            except HerdrError:
                found.processes[pane_id] = {}
        process = claude_process(found.processes[pane_id])
        argv = process.get("argv") if process is not None else None
        if isinstance(argv, list) and session_id_in([arg for arg in argv if isinstance(arg, str)]) == session_id:
            return pane
    return None


async def _close(
    herdr: Herdr, pane: dict | None, found: _Found, what: str = "the shell left"
) -> tuple[bool, str]:
    """Close what herdr left of an agent, if anything: whether it's gone (True when there was
    none), and a note for the log line either way, so the log says which pane went and why."""
    if pane is None:
        return True, ""
    found.closed.add(pane["pane_id"])
    try:
        await herdr.close_pane(pane["pane_id"])
    except HerdrError as error:
        return False, f" (couldn't close {what} in {pane['pane_id']}: {error})"
    return True, f" (closed {what} in {pane['pane_id']})"


async def _adopt(path: Path, entry: SavedAgent, resumed: dict, found: _Found, stopped: set[str]) -> None:
    """Take `resumed`, which runs `entry`'s conversation, as the agent's own pane from now on."""
    # First: a write that fails mustn't let a later entry close the pane.
    found.claimed.add(resumed["terminal_id"])
    found.sessions.add(entry.session_id)
    kept = entry.model_copy(
        update={"pane_id": resumed["pane_id"], "terminal_id": resumed["terminal_id"], "misses": 0,
                "restore_failed": False}
    )
    try:
        await asyncio.to_thread(_replace, path, entry.terminal_id, kept)
    except (OSError, StateError):
        # As for a restored agent: the old terminal is gone for good, so the watcher's next
        # write drops the stale entry, whose old pane id now another terminal's would make the
        # next round look like a herdr restart, and saves the pane from its snapshot.
        stopped.add(entry.terminal_id)
        raise


async def _forget(path: Path, entry: SavedAgent, stopped: set[str]) -> None:
    """Take `entry` out of agents.json for good."""
    try:
        await asyncio.to_thread(forget, path, entry.terminal_id)
    except (OSError, StateError):
        # As for a restored agent whose entry couldn't be written: the watcher's next write
        # drops it, whose old pane id, should a new server hand it to another terminal, would
        # make the next round look like a herdr restart and start the agent again.
        stopped.add(entry.terminal_id)
        raise


async def _keep_failed(path: Path, entry: SavedAgent) -> None:
    """Mark `entry` as one the restore couldn't start, so the snapshots leave it be. Its misses
    go back to 0: an earlier miss would otherwise get it forgotten at the next restart."""
    failed = entry.model_copy(update={"restore_failed": True, "misses": 0})
    await asyncio.to_thread(_replace, path, entry.terminal_id, failed)


def _session_in(pane: dict, found: _Found) -> str | None:
    """The conversation a Claude pane runs, as a save would record it: herdr's hook says,
    else its command line (a conversation resumed in a config without the hook)."""
    running = found.live.get(pane.get("pane_id"))
    if running is not None:
        return running.session_id
    session = pane.get("agent_session")
    return session.get("value") if isinstance(session, dict) else None


def _replace(path: Path, terminal_id: str, restarted: SavedAgent) -> None:
    """Put `restarted` where the entry for `terminal_id` was, or at the end if it's gone."""
    with locked(path):
        saved = load(path, for_writing=True)
        # Any other entry with the new terminal id is a stale duplicate; the one being
        # replaced may share it (a failed start keeps its id) and stays where it is.
        agents = [
            agent for agent in saved.agents
            if agent.terminal_id != restarted.terminal_id or agent.terminal_id == terminal_id
        ]
        at = next((i for i, agent in enumerate(agents) if agent.terminal_id == terminal_id), None)
        if at is None:
            agents.append(restarted)
        else:
            agents[at] = restarted
        save(path, saved.model_copy(update={"agents": agents}))


async def watch(
    service: AgentService,
    herdr: Herdr,
    path: Path,
    interval_s: float = 30,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Keep `path` current with the agents herdr runs, and restore the saved ones whenever
    herdr's server has restarted (see `_restarted`). A restore that didn't finish is run
    again next round. Runs until cancelled."""
    # None until herdr first answers; then whether it answered in the last round.
    answering: bool | None = None
    pending = False
    while True:
        try:
            seen = await snapshot(herdr)
            # Answering only once the check has gone through: one that fails on herdr's first
            # round must be made again as a first round, or a lost layout goes unnoticed.
            pending = pending or await _restarted(herdr, path, answering)
            answering = True
            if pending:
                await restore(service, herdr, path)
                # Only when it ran through: one cut short by an error starts over next round.
                pending = False
                seen = await snapshot(herdr)
                restarted = True
            else:
                restarted = False
            await asyncio.to_thread(_write, path, seen, restarted, service.stopped)
        except HerdrError as error:
            if error.code != SERVER_NOT_RUNNING:
                # Its server still runs (a timeout, output it couldn't parse): only this round
                # failed. Counted as not answering, it would make the next round look like a
                # new server, and bring back an agent closed by hand meanwhile.
                log.warning("Saving the running agents failed (%s); trying again in %g s.", error, interval_s)
            else:
                if answering:
                    log.warning("herdr doesn't answer (%s); agents will be restored when it does.", error)
                answering = False
        except Exception:  # noqa: BLE001 - one bad round mustn't end the watching
            log.exception("Saving the running agents failed; trying again in %g s.", interval_s)
        await sleep(interval_s)


async def _restarted(herdr: Herdr, path: Path, was_answering: bool | None) -> bool:
    """Whether herdr's server is a new one. It is when a saved agent's pane id is there with
    another terminal id: herdr brings panes back under their ids, with new terminals. It is
    too when herdr answers for the first time, or again after not answering, and none of the
    saved agents' terminals is there.

    A pane closed by hand is gone entirely, which says nothing. Agents a restore failed to
    start keep stale ids by design, so they don't count."""
    fresh = was_answering is not True
    running = [agent for agent in await asyncio.to_thread(_saved, path) if not agent.restore_failed]
    if not running:
        return fresh
    panes = await herdr.list_panes()
    terminals = {pane.get("terminal_id") for pane in panes}
    by_pane = {pane.get("pane_id"): pane.get("terminal_id") for pane in panes}
    if any(by_pane.get(agent.pane_id, agent.terminal_id) != agent.terminal_id for agent in running):
        return True
    return fresh and not any(agent.terminal_id in terminals for agent in running)


def _has_conversation_on_disk(config_dir: Path | None, session_id: str) -> bool:
    """Whether Claude Code has a conversation `session_id` in that config: it keeps each at
    <config>/projects/<folder>/<id>.jsonl, the folder named after where it ran."""
    projects = (config_dir or Path.home() / ".claude") / "projects"
    try:
        return any(projects.glob(f"*/{session_id}.jsonl"))
    except OSError:
        # Can't tell: resuming is what a running agent with that id would want.
        return True


# Looked up through the module so tests can stand in for the disk.
_has_conversation = _has_conversation_on_disk


def _saved(path: Path) -> list[SavedAgent]:
    """The saved agents, read under the file's lock: on Windows a read while another process
    replaces the file fails. In a thread: the lock may be waited for. A read that fails
    anyway raises: taken for no saved agents, it would end a restore that restored nothing,
    or answer the restart check wrongly."""
    with locked(path):
        return load(path, must_read=True).agents


def _write(path: Path, seen: list[SavedAgent], restarted: bool, stopped: set[str] = frozenset()) -> None:
    with locked(path):
        # Agents stopped through the bridge since the snapshot began are gone for good. Read
        # under the lock: stop_agent adds to `stopped` before its forget takes the lock, so
        # either it's in the set now or its forget runs after this write.
        gone = frozenset(stopped)
        seen = [agent for agent in seen if agent.terminal_id not in gone]
        saved = load(path, for_writing=True)
        # Also saved ones: a restore may have written an agent that a stop then closed before
        # its forget could find it.
        kept = [agent for agent in saved.agents if agent.terminal_id not in gone]
        save(path, saved.model_copy(update={"agents": merge(kept, seen, restarted)}))
