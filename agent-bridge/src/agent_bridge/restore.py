"""Bring back the Claude Code agents saved in `agents.json` after the host or herdr's server
restarts: each in a workspace of its own, on its account, with its arguments and, when it
can, its conversation. A restored agent is never sent a prompt.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from agent_bridge.agents import AgentService
from agent_bridge.errors import BridgeError
from agent_bridge.herdr import Herdr, HerdrError
from agent_bridge.saved_agents import SavedAgent, forget, load, locked, merge, save, snapshot

log = logging.getLogger(__name__)


async def restore(service: AgentService, herdr: Herdr, path: Path) -> list[str]:
    """Start each saved agent that isn't running, one after another in the file's order.
    Each outcome is logged; the lines are returned too. A HerdrError from listing herdr's
    panes or agents propagates: herdr isn't up."""
    entries = load(path).agents
    panes = await herdr.list_panes()
    # What runs in each Claude pane, to tell whether herdr resumed a conversation as it was.
    live = {agent.pane_id: agent for agent in await snapshot(herdr)}
    closed: set[str] = set()
    lines = []
    for entry in entries:
        line, failed = await _restore_one(service, herdr, path, entry, entries, panes, live, closed)
        (log.warning if failed else log.info)("%s", line)
        lines.append(line)
    return lines


async def _restore_one(
    service: AgentService,
    herdr: Herdr,
    path: Path,
    entry: SavedAgent,
    entries: list[SavedAgent],
    panes: list[dict],
    live: dict[str, SavedAgent],
    closed: set[str],
) -> tuple[str, bool]:
    """The log line for `entry`, and whether it says something failed."""
    label = entry.name or entry.cwd
    if entry.misses > 0:
        # It had already disappeared while this herdr server ran: stopped or closed, not lost.
        return f"{label}: not restored, it had gone before the restart; left as it is", False
    for pane in panes:
        if pane.get("terminal_id") == entry.terminal_id:
            return f"{label}: still running in {pane['pane_id']}, left as it is", False
    open_panes = [pane for pane in panes if pane["pane_id"] not in closed]
    resumed = next(
        (pane for pane in open_panes if entry.session_id and _session_of(pane) == entry.session_id), None
    )
    if resumed is not None:
        found = live.get(resumed["pane_id"])
        if found is not None and (found.config_dir, found.args) == (entry.config_dir, entry.args):
            return f"{label}: herdr resumed it in {resumed['pane_id']} as it was, left as it is", False
        # herdr's own resume runs plain `claude --resume <id>`: without the account's
        # CLAUDE_CONFIG_DIR or the agent's arguments.
        stale = resumed
    else:
        # herdr brings an agent's pane back as a plain shell; it keeps its pane id.
        stale = next((pane for pane in open_panes if pane["pane_id"] == entry.pane_id and not pane.get("agent")), None)
    note = ""
    if stale is not None:
        closed.add(stale["pane_id"])
        try:
            await herdr.close_pane(stale["pane_id"])
        except HerdrError as error:
            note = f" (couldn't close pane {stale['pane_id']}: {error})"

    if entry.session_id:
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
            await asyncio.to_thread(forget, path, entry.terminal_id)
            return f"{label}: not restored, and forgotten: {error.message}{note}", True
        await _keep_failed(path, entry)
        return f"{label}: not restored ({error.code}: {error.message}), kept for the next restart{note}", True
    except Exception as error:  # noqa: BLE001 - one agent's failure mustn't stop the others
        # Any other failure is likely passing (herdr busy, a slow start): try again next time.
        await _keep_failed(path, entry)
        return f"{label}: not restored ({error}), kept for the next restart{note}", True

    restarted = entry.model_copy(
        update={"pane_id": agent["pane_id"], "terminal_id": agent["terminal_id"], "misses": 0,
                "restore_failed": False}
    )
    await asyncio.to_thread(_replace, path, entry.terminal_id, restarted)
    return f"{label}: restored in {agent['pane_id']}, {how}{note}", False


async def _keep_failed(path: Path, entry: SavedAgent) -> None:
    """Mark `entry` as one the restore couldn't start, so the snapshots leave it be."""
    await asyncio.to_thread(_replace, path, entry.terminal_id, entry.model_copy(update={"restore_failed": True}))


def _session_of(pane: dict) -> str | None:
    session = pane.get("agent_session")
    return session.get("value") if isinstance(session, dict) else None


def _replace(path: Path, terminal_id: str, restarted: SavedAgent) -> None:
    """Put `restarted` where the entry for `terminal_id` was, or at the end if it's gone."""
    with locked(path):
        saved = load(path)
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
            was, answering = answering, True
            pending = pending or await _restarted(herdr, path, was)
            if pending:
                await restore(service, herdr, path)
                # Only when it ran through: one cut short by an error starts over next round.
                pending = False
                seen = await snapshot(herdr)
                restarted = True
            else:
                restarted = False
            # Agents stopped through the bridge since the snapshot began are gone for good.
            stopped = service.stopped
            await asyncio.to_thread(_write, path, [agent for agent in seen if agent.terminal_id not in stopped], restarted)
        except HerdrError as error:
            if answering:
                log.warning("herdr doesn't answer (%s); agents will be restored when it does.", error)
            answering = False
        except Exception:  # noqa: BLE001 - one bad round mustn't end the watching
            log.exception("Saving the running agents failed; trying again in %g s.", interval_s)
        await sleep(interval_s)


async def _restarted(herdr: Herdr, path: Path, was_answering: bool | None) -> bool:
    """Whether herdr's server is a new one: it answers for the first time; or a saved agent's
    pane id is there with another terminal id (herdr brings panes back under their ids, with
    new terminals); or it answers again after not answering and none of the saved agents has
    its terminal. A pane closed by hand is gone entirely, which says nothing. Agents a
    restore failed to start keep stale ids by design, so they don't count."""
    if was_answering is None:
        return True
    running = [agent for agent in await asyncio.to_thread(_saved, path) if not agent.restore_failed]
    if not running:
        return was_answering is False
    panes = await herdr.list_panes()
    terminals = {pane.get("terminal_id") for pane in panes}
    by_pane = {pane.get("pane_id"): pane.get("terminal_id") for pane in panes}
    if any(by_pane.get(agent.pane_id, agent.terminal_id) != agent.terminal_id for agent in running):
        return True
    return was_answering is False and not any(agent.terminal_id in terminals for agent in running)


def _saved(path: Path) -> list[SavedAgent]:
    return load(path).agents


def _write(path: Path, seen: list[SavedAgent], restarted: bool) -> None:
    with locked(path):
        saved = load(path)
        save(path, saved.model_copy(update={"agents": merge(saved.agents, seen, restarted)}))
