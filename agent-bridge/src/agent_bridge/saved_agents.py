"""The Claude Code agents running in herdr, kept in `agents.json` so they can come back after
the host restarts.

`serve` snapshots them every so often; the file keeps an agent for one snapshot that misses
it, so a pane herdr failed to list once isn't forgotten.
"""

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, ValidationError

from agent_bridge import state
from agent_bridge.claude_args import clean_args, session_id_in
from agent_bridge.claude_process import claude_environ, claude_process
from agent_bridge.herdr import Herdr, HerdrError
from agent_bridge.state import state_file

log = logging.getLogger(__name__)

# An entry unseen in this many snapshots in a row is dropped.
MAX_MISSES = 2
LOCK_WAIT_SECONDS = 10
LOCK_RETRY_SECONDS = 0.1
# A lock older than this was left by a process that died.
STALE_LOCK_SECONDS = 30


class SavedAgent(BaseModel):
    name: str | None
    cwd: str
    # CLAUDE_CONFIG_DIR of the agent; None for the default config, or when unreadable.
    config_dir: str | None
    config_dir_unknown: bool = False
    # What Claude was started with after `claude`, without session flags or a prompt.
    args: list[str]
    session_id: str | None
    pane_id: str
    # Identifies the agent across snapshots: pane ids are reused after a restart.
    terminal_id: str
    # Snapshots in a row that didn't see it.
    misses: int = 0


class SavedAgents(BaseModel):
    version: int = 1
    agents: list[SavedAgent] = []


def agents_file() -> Path:
    return state_file().with_name("agents.json")


def load(path: Path) -> SavedAgents:
    """The saved agents; empty when there are none, or the file can't be used (warned)."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return SavedAgents()
    except OSError as error:
        log.warning("Can't read %s (%s); no agents will be restored from it.", path, error.strerror)
        return SavedAgents()
    try:
        saved = SavedAgents.model_validate_json(data)
    except ValidationError:
        log.warning("%s isn't a list of saved agents; no agents will be restored from it.", path)
        return SavedAgents()
    if saved.version > SavedAgents().version:
        log.warning("%s is from a newer agent-bridge; no agents will be restored from it.", path)
        return SavedAgents()
    return saved


def save(path: Path, saved: SavedAgents) -> None:
    state.save(path, saved)


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Holds `<path>.lock`, so the bridge and a restore from the command line don't write
    the file over each other."""
    lock = path.with_name(f"{path.name}.lock")
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            break
        except FileExistsError:
            pass
        try:
            if time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
                lock.unlink(missing_ok=True)
                continue
        except FileNotFoundError:
            continue
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{lock} is held by another process.")
        time.sleep(LOCK_RETRY_SECONDS)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


async def snapshot(herdr: Herdr) -> list[SavedAgent]:
    """One entry per Claude agent herdr lists. A HerdrError from listing propagates (herdr
    is unreachable); a pane whose processes can't be read is left out."""
    found = []
    for agent in await herdr.list_agents():
        if agent.get("agent") != "claude":
            continue
        try:
            info = await herdr.process_info(agent["pane_id"])
        except HerdrError:
            continue
        process = claude_process(info)
        if process is None:
            continue
        argv = process.get("argv")
        argv = [arg for arg in argv if isinstance(arg, str)] if isinstance(argv, list) else []
        # Off the event loop: reading another process's memory may block, on Windows above all.
        environ = await asyncio.to_thread(claude_environ, info)
        session = agent.get("agent_session")
        if isinstance(session, dict) and session.get("kind") == "id" and session.get("value"):
            session_id = session["value"]
        else:
            session_id = session_id_in(argv)
        found.append(
            SavedAgent(
                name=agent.get("name"),
                cwd=agent["cwd"],
                config_dir=(environ.get("CLAUDE_CONFIG_DIR") or None) if environ is not None else None,
                config_dir_unknown=environ is None,
                args=clean_args(argv[1:]),
                session_id=session_id,
                pane_id=agent["pane_id"],
                terminal_id=agent["terminal_id"],
            )
        )
    return found


def merge(saved: list[SavedAgent], seen: list[SavedAgent], restarted: bool) -> list[SavedAgent]:
    """`seen` replaces what it matches; an agent not seen counts a miss (not after a restart,
    when none of them could be seen) and goes at MAX_MISSES."""
    seen_ids = {agent.terminal_id for agent in seen}
    merged = [agent.model_copy(update={"misses": 0}) for agent in seen]
    for agent in saved:
        if agent.terminal_id in seen_ids:
            continue
        misses = agent.misses if restarted else agent.misses + 1
        if misses < MAX_MISSES:
            merged.append(agent.model_copy(update={"misses": misses}))
    return merged


def forget(path: Path, terminal_id: str) -> None:
    with locked(path):
        saved = load(path)
        save(path, saved.model_copy(update={
            "agents": [agent for agent in saved.agents if agent.terminal_id != terminal_id]
        }))
