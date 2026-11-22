"""Bring back the Claude Code agents saved in `agents.json` after the host or herdr's server
restarts: each in a workspace of its own, on its account, with its arguments and, when it
can, its conversation. A restored agent is never sent a prompt.
"""

import asyncio
import logging
from pathlib import Path

from agent_bridge.agents import AgentService
from agent_bridge.errors import BridgeError
from agent_bridge.herdr import Herdr, HerdrError
from agent_bridge.saved_agents import SavedAgent, forget, load, locked, save, snapshot

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
        return f"{label}: not restored ({error.code}: {error.message}), kept for the next restart{note}", True
    except Exception as error:  # noqa: BLE001 - one agent's failure mustn't stop the others
        # Any other failure is likely passing (herdr busy, a slow start): try again next time.
        return f"{label}: not restored ({error}), kept for the next restart{note}", True

    restarted = entry.model_copy(
        update={"pane_id": agent["pane_id"], "terminal_id": agent["terminal_id"], "misses": 0}
    )
    await asyncio.to_thread(_replace, path, entry.terminal_id, restarted)
    return f"{label}: restored in {agent['pane_id']}, {how}{note}", False


def _session_of(pane: dict) -> str | None:
    session = pane.get("agent_session")
    return session.get("value") if isinstance(session, dict) else None


def _replace(path: Path, terminal_id: str, restarted: SavedAgent) -> None:
    """Put `restarted` where the entry for `terminal_id` was, or at the end if it's gone."""
    with locked(path):
        saved = load(path)
        agents = [agent for agent in saved.agents if agent.terminal_id != restarted.terminal_id]
        at = next((i for i, agent in enumerate(agents) if agent.terminal_id == terminal_id), None)
        if at is None:
            agents.append(restarted)
        else:
            agents[at] = restarted
        save(path, saved.model_copy(update={"agents": agents}))
