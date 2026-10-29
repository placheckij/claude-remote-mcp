"""What the API does with agents on top of herdr: path and count limits, folder trust,
waiting for a turn to settle, and trimming terminal output.

Agents are addressed by their herdr pane id (e.g. `w1:p1`).
"""

import asyncio
import re
import unicodedata
from collections.abc import Awaitable
from pathlib import Path

from agent_bridge.config import Settings
from agent_bridge.errors import BridgeError
from agent_bridge.herdr import Herdr, HerdrError
from agent_bridge.schemas import AgentSummary, Output, PromptResult, cap, summarize
from agent_bridge.screen import SNAPSHOT_LINES, last_lines
from agent_bridge.trust import TrustError, trust_folder

BUSY_MESSAGE = (
    "The agent is still working on an earlier request, so the prompt was not sent. Wait for "
    "it with wait_for_agent, then send the prompt again."
)

# How long a wait with expect_change gives the agent to leave its old state; herdr's own
# `agent prompt --wait` uses the same window.
STATE_CHANGE_WINDOW_MS = 5000


class AgentService:
    def __init__(self, settings: Settings, herdr: Herdr):
        self.settings = settings
        self.herdr = herdr
        # Serializes count-then-start and picking a free name, so concurrent requests can
        # neither exceed the limit nor give two agents the same name.
        self._start_lock = asyncio.Lock()
        # One prompt at a time per agent; a second one is refused rather than typed in.
        self._prompt_locks: dict[str, asyncio.Lock] = {}

    async def list_agents(self) -> list[AgentSummary]:
        return [summarize(agent) for agent in await self.herdr.list_agents()]

    async def get_agent(self, agent_id: str) -> AgentSummary:
        return summarize(await self.herdr.get_agent(agent_id))

    async def read_output(self, agent_id: str, lines: int, source: str) -> Output:
        snapshot = await self.herdr.read(agent_id, SNAPSHOT_LINES, source)
        return cap(last_lines(snapshot, lines), self.settings.max_output_chars)

    async def prompt(
        self, agent_id: str, text: str, timeout_s: int | None, lines: int
    ) -> PromptResult:
        lock = self._prompt_locks.setdefault(agent_id, asyncio.Lock())
        if lock.locked():
            raise BridgeError(409, "agent_busy", BUSY_MESSAGE)
        async with lock:
            status = (await self._controllable(agent_id)).status
            # herdr types the text and presses Enter; on a pending question that Enter picks
            # the highlighted answer (often "Yes") and the text is lost. The agent could still
            # change state between this check and the prompt; that window is accepted.
            if status == "blocked":
                raise BridgeError(
                    409,
                    "agent_blocked",
                    "The agent is waiting for an answer to a question, so the prompt was not "
                    "sent. Read the question with read_output and answer it with answer_agent "
                    "first.",
                )
            # herdr's --wait can't tell turns apart, so it would report the turn already
            # running as this prompt's result.
            if status == "working":
                raise BridgeError(409, "agent_busy", BUSY_MESSAGE)
            waiting = self.herdr.prompt(agent_id, text, self._timeout_ms(timeout_s))
            return await self._settle(agent_id, waiting, lines)

    async def wait(
        self, agent_id: str, timeout_s: int | None, lines: int, expect_change: bool
    ) -> PromptResult:
        waiting = self._wait(agent_id, self._timeout_ms(timeout_s), expect_change)
        return await self._settle(agent_id, waiting, lines)

    async def send_keys(self, agent_id: str, keys: list[str]) -> None:
        await self._controllable(agent_id)
        await self.herdr.send_keys(agent_id, keys)

    async def start_agent(self, cwd: str, name: str) -> AgentSummary:
        folder = self._allowed_folder(cwd)
        async with self._start_lock:
            running = await self._check_agent_limit()
            name = herdr_name(name, {agent.get("name") for agent in running})
            await self._trust(folder)
            agent = await self._start_with_one_retry(name, folder)
        return summarize(agent)

    async def _start_with_one_retry(self, name: str, folder: Path) -> dict:
        # herdr 0.8.0 once failed a start with "expected claude, detected " (no agent seen
        # in the pane yet); 20 later tries all worked. The failed attempt's workspace is
        # already closed, so one more try in a fresh workspace is safe.
        for attempt in (1, 2):
            try:
                return await self.herdr.start_agent(
                    name, str(folder), self.settings.claude_env(), self.settings.claude_args
                )
            except HerdrError as error:
                if error.code == "folder_not_trusted":
                    raise BridgeError(409, error.code, self._untrusted_message(folder)) from error
                if not error.message.startswith("expected claude, detected"):
                    raise
                if attempt == 2:
                    raise BridgeError(
                        502,
                        "agent_not_detected",
                        "Claude Code didn't come up in the new workspace, twice, so the "
                        "workspaces were closed. Try starting the agent again in a moment.",
                    ) from error
        raise AssertionError("unreachable")

    async def rename_agent(self, agent_id: str, name: str) -> AgentSummary:
        """Give the agent and its workspace the same new name, as start_agent does."""
        agent = await self._controllable(agent_id)
        async with self._start_lock:
            running = await self.herdr.list_agents()
            name = herdr_name(name, {a.get("name") for a in running if a["pane_id"] != agent_id})
            renamed = await self.herdr.rename_agent(agent_id, name)
            await self.herdr.rename_workspace(agent.workspace_id, name)
        return summarize(renamed)

    async def stop_agent(self, agent_id: str) -> None:
        # Looking the agent up first also refuses panes that aren't agents (a shell, a dev
        # server): herdr answers agent_not_found, a 404.
        await self._controllable(agent_id)
        await self.herdr.close_pane(agent_id)

    async def _controllable(self, agent_id: str) -> AgentSummary:
        """The agent, if it works under the allowed roots. Every herdr agent can be read,
        but only those may be prompted, answered, renamed or stopped."""
        agent = await self.get_agent(agent_id)
        if not agent.cwd or not self.settings.is_allowed(Path(agent.cwd)):
            raise BridgeError(
                403,
                "agent_not_allowed",
                f"Agent {agent_id} works in {agent.cwd or 'an unknown folder'}, outside the "
                "allowed folders, so it can be read but not prompted, answered, renamed or "
                "stopped.",
            )
        return agent

    def _timeout_ms(self, timeout_s: int | None) -> int:
        return timeout_s * 1000 if timeout_s else self.settings.prompt_timeout_ms

    async def _wait(self, agent_id: str, timeout_ms: int, expect_change: bool) -> dict:
        remaining = timeout_ms
        if expect_change:
            window = min(STATE_CHANGE_WINDOW_MS, timeout_ms)
            try:
                await self.herdr.wait(agent_id, window, until=("working",))
            except HerdrError as error:
                # No change seen: the turn may already be over, or the answer did not
                # take; either way the settled wait below reports the current state.
                if error.code != "timeout":
                    raise
            remaining -= window
            if remaining < 1000:
                # The window used up the budget (herdr waits at least 1 s): report the
                # state now rather than overrun the caller's timeout.
                return await self.herdr.get_agent(agent_id)
        return await self.herdr.wait(agent_id, max(remaining, 1000))

    async def _settle(self, agent_id: str, waiting: Awaitable[dict], lines: int) -> PromptResult:
        """Await `waiting` (a herdr wait), then report status and the latest output."""
        timed_out = unconfirmed = False
        try:
            agent = await waiting
        except HerdrError as error:
            if error.code not in ("timeout", "agent_prompt_stalled"):
                raise
            timed_out = error.code == "timeout"
            unconfirmed = error.code == "agent_prompt_stalled"
            agent = await self.herdr.get_agent(agent_id)
        output = last_lines(await self.herdr.read(agent_id, SNAPSHOT_LINES), lines)
        return PromptResult(
            agent=summarize(agent),
            output=cap(output, self.settings.max_output_chars),
            timed_out=timed_out,
            unconfirmed=unconfirmed,
        )

    def _allowed_folder(self, cwd: str) -> Path:
        folder = Path(cwd).expanduser().resolve()
        if not self.settings.is_allowed(folder):
            raise BridgeError(403, "path_not_allowed", f"{folder} is outside the allowed roots")
        if not folder.is_dir():
            raise BridgeError(404, "not_a_directory", f"{folder} is not a directory")
        return folder

    async def _check_agent_limit(self) -> list[dict]:
        """The running agents, unless there are already as many as the limit allows."""
        running = await self.herdr.list_agents()
        limit = self.settings.max_agents
        if len(running) >= limit:
            raise BridgeError(
                409,
                "agent_limit_reached",
                f"Refusing to start a new agent: {len(running)} agents are already running and "
                f"the limit is {limit}. Stop an agent first.",
            )
        return running

    async def _trust(self, folder: Path) -> None:
        trust_root = self.settings.trust_root
        if not trust_root or not folder.is_relative_to(trust_root.expanduser().resolve()):
            return
        # Without a config dir agents use the default config, which trust_folder always
        # refuses to edit.
        config_dir = self.settings.claude_config_dir or Path.home()
        try:
            await asyncio.to_thread(trust_folder, config_dir, folder, trust_root)
        except TrustError as error:
            raise BridgeError(409, "trust_refused", str(error)) from error

    def _untrusted_message(self, folder: Path) -> str:
        """Why Claude Code doesn't trust `folder`, and what would fix it."""
        head = f"Claude Code does not trust {folder}, so the agent was not started."
        if self.settings.trust_root is None:
            return (
                f"{head} Automatic trust is off: trust the folder once by opening Claude Code "
                "there, or set AGENT_BRIDGE_TRUST_ROOT and AGENT_BRIDGE_CLAUDE_CONFIG_DIR."
            )
        return (
            f"{head} It is outside the trust root {self.settings.trust_root}: trust it once by "
            "opening Claude Code there."
        )


# herdr's rule for agent names: lower-case ASCII, starting with a letter, at most 32 long.
HERDR_NAME_MAX = 32


def herdr_name(spoken: str, taken: set[str | None]) -> str:
    """A name herdr accepts for `spoken` (e.g. "Grafik fix!" → "grafik-fix"), made unique
    among the `taken` names of live agents by a "-2", "-3", … suffix."""
    # NFKD splits accents off most letters; ł has no decomposition, so map it by hand.
    ascii_name = (
        unicodedata.normalize("NFKD", spoken.replace("ł", "l").replace("Ł", "L"))
        .encode("ascii", "ignore")
        .decode()
        .lower()
    )
    base = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9_-]+", "-", ascii_name)).strip("-_")
    if not base[:1].isalpha():
        base = f"agent-{base}".rstrip("-")
    base = base[:HERDR_NAME_MAX].rstrip("-_")
    name, number = base, 1
    while name in taken:
        number += 1
        suffix = f"-{number}"
        name = base[: HERDR_NAME_MAX - len(suffix)].rstrip("-_") + suffix
    return name

