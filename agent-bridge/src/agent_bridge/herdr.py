"""Thin async wrapper around the herdr CLI.

Herdr is the source of truth for agents; this module only shells out to it and
parses its JSON envelopes (`{"result": ...}` or `{"error": {"code", "message"}}`).
"""

import asyncio
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import NamedTuple

Runner = Callable[[Sequence[str]], Awaitable[tuple[int, str, str]]]

SHELL_READY_TIMEOUT_S = 15
SHELL_READY_POLL_S = 0.5
TRUST_DIALOG_TEXT = "Yes, I trust this folder"
# What Claude Code shows at startup when the account's organization turns Remote Control
# off; the session then runs without it. Short, so a narrow pane doesn't wrap it.
REMOTE_CONTROL_BLOCKED_TEXT = "Remote Control is disabled"

WINDOWS = sys.platform == "win32"
# Windows hands a program its arguments as one command line of at most 32,767 UTF-16
# units, the closing NUL included. Python quotes the arguments into it, so a `"` takes two
# (`\"`), and so does an emoji: a 20,000-character prompt of quotes doesn't fit.
WINDOWS_COMMAND_LINE_MAX = 32_766
# What Windows reports for a longer one, as a FileNotFoundError.
ERROR_FILENAME_EXCED_RANGE = 206
# Run without a console of its own (agent-bridgew), agent-bridge would otherwise give each
# herdr a new console window on the desktop.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}


def command_line_length(argv: Sequence[str]) -> int:
    """How many of Windows' command-line units `argv` takes once quoted."""
    return len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2


class Started(NamedTuple):
    agent: dict
    # Whether the notice start_agent was asked to watch for was on screen.
    notice_shown: bool = False


class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


async def subprocess_runner(argv: Sequence[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **NO_WINDOW,
    )
    try:
        stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        # The caller gave up (e.g. the HTTP client disconnected); don't leave herdr running.
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


@dataclass
class Herdr:
    binary: str = "herdr"
    runner: Runner = subprocess_runner

    async def _run(self, *args: str) -> str:
        try:
            code, stdout, stderr = await self.runner([self.binary, *args])
        except FileNotFoundError as error:
            if getattr(error, "winerror", None) == ERROR_FILENAME_EXCED_RANGE:
                raise HerdrError(
                    "command_too_long",
                    "herdr could not be run: its command line is longer than Windows allows.",
                ) from None
            raise HerdrError(
                "herdr_not_found",
                f"herdr was not found at {self.binary}. Install it, or set "
                "AGENT_BRIDGE_HERDR_BIN to its full path.",
            ) from None
        except OSError as error:
            raise HerdrError("herdr_not_runnable", f"herdr could not be run: {error}") from None
        if code != 0:
            # herdr writes its {"error": ...} envelope to stderr; older paths use stdout.
            raise (
                _parse_error(stderr)
                or _parse_error(stdout)
                or HerdrError("cli_failed", (stderr or stdout).strip())
            )
        return stdout

    async def _json(self, *args: str) -> dict:
        stdout = await self._run(*args)
        if not stdout.strip():  # some commands (e.g. `pane run`) print nothing on success
            return {}
        try:
            envelope = json.loads(stdout)
        except ValueError:
            first_line = stdout.strip().splitlines()[0][:200]
            raise HerdrError(
                "bad_output", f"herdr answered with something other than JSON: {first_line}"
            ) from None
        if "error" in envelope:
            raise HerdrError(envelope["error"]["code"], envelope["error"]["message"])
        return envelope["result"]

    async def version(self) -> str:
        """herdr's version, e.g. "0.9.5" from `herdr 0.9.5`."""
        return (await self._run("--version")).strip().removeprefix("herdr").strip()

    async def server_status(self) -> dict:
        """`herdr status server --json`: whether its server is `running`, its `version`, and
        `restart_needed` (herdr 0.8: another version than this herdr; from 0.9: one this herdr
        can't work with)."""
        stdout = await self._run("status", "server", "--json")
        try:
            status = json.loads(stdout)
        except ValueError:
            status = None
        if not isinstance(status, dict):
            raise HerdrError("bad_output", f"herdr's server status isn't a JSON object: {stdout.strip()[:200]}")
        return status

    async def list_agents(self) -> list[dict]:
        return (await self._json("agent", "list"))["agents"]

    async def get_agent(self, target: str) -> dict:
        return (await self._json("agent", "get", target))["agent"]

    async def process_info(self, pane_id: str) -> dict:
        """The pane's foreground processes, each with its pid, name and argv0; {} if herdr
        doesn't say."""
        return (await self._json("pane", "process-info", "--pane", pane_id)).get("process_info") or {}

    async def read(self, target: str, lines: int, source: str = "recent") -> str:
        """Read terminal text. While the agent is working or blocked herdr can only capture
        the visible screen, which is then what callers get (it holds the live question)."""
        try:
            return await self._read(target, lines, source)
        except HerdrError as error:
            if error.code != "agent_not_idle" or source == "visible":
                raise
            return await self._read(target, lines, "visible")

    async def _read(self, target: str, lines: int, source: str) -> str:
        return await self._run(
            "agent", "read", target, "--lines", str(lines), "--source", source, "--format", "text"
        )

    async def prompt(self, target: str, text: str, timeout_ms: int) -> dict:
        """Submit a prompt and wait until the agent settles (idle, done or blocked)."""
        args = ("agent", "prompt", target, text, "--wait", "--timeout", str(timeout_ms))
        if WINDOWS:
            length = command_line_length([self.binary, *args])
            if length > WINDOWS_COMMAND_LINE_MAX:
                raise HerdrError(
                    "prompt_too_long",
                    f"The prompt is too long to hand to herdr on Windows: quoted, its command "
                    f"line takes {length:,} characters and Windows allows "
                    f"{WINDOWS_COMMAND_LINE_MAX:,}. Double quotes, backslashes before them and "
                    "emoji count twice. "
                    "Send it in shorter parts.",
                )
        return (await self._json(*args))["agent"]

    async def wait(self, target: str, timeout_ms: int, until: Sequence[str] = ()) -> dict:
        """Wait for one of `until`, or by default until the agent settles (idle, done, blocked)."""
        until_args = [arg for status in until for arg in ("--until", status)]
        result = await self._json("agent", "wait", target, *until_args, "--timeout", str(timeout_ms))
        return result["agent"]

    async def send_keys(self, target: str, keys: Sequence[str]) -> None:
        await self._json("agent", "send-keys", target, *keys)

    async def rename_agent(self, target: str, name: str) -> dict:
        return (await self._json("agent", "rename", target, name))["agent"]

    async def rename_workspace(self, workspace_id: str, label: str) -> None:
        await self._json("workspace", "rename", workspace_id, label)

    async def start_agent(
        self,
        name: str,
        cwd: str,
        env: dict[str, str],
        agent_args: Sequence[str],
        timeout_ms: int = 60_000,
        notice: str | None = None,
    ) -> Started:
        """Create a workspace in `cwd` and start Claude Code in its root pane. With `notice`,
        also say whether Claude Code showed that text while the trust dialog was looked for."""
        env_args = [arg for key, value in env.items() for arg in ("--env", f"{key}={value}")]
        created = await self._json(
            "workspace", "create", "--cwd", cwd, "--label", name, "--no-focus", *env_args
        )
        workspace_id = created["workspace"]["workspace_id"]
        pane_id = created["root_pane"]["pane_id"]
        untrusted = HerdrError("folder_not_trusted", f"Claude Code does not trust {cwd}")
        try:
            try:
                started = await self._start_when_shell_ready(name, pane_id, timeout_ms, agent_args)
            except HerdrError as error:
                # On Windows herdr calls an agent on the trust dialog blocked, so the start
                # fails as not ready during startup.
                if error.code != "agent_not_ready":
                    raise
                try:
                    shown = await self._on_screen(pane_id, TRUST_DIALOG_TEXT)
                except HerdrError:
                    raise error from None  # the start's failure, not the probe's
                if shown == TRUST_DIALOG_TEXT:
                    raise untrusted from error
                raise
            # On macOS herdr reports the agent ready while Claude's folder-trust dialog is
            # on screen, and that dialog defaults to "No, exit": a prompt sent now would
            # quit Claude. Refuse instead of leaving a trap. The notice is looked for in the
            # same wait, so it costs no extra time.
            shown = await self._on_screen(pane_id, TRUST_DIALOG_TEXT, notice)
            if shown == TRUST_DIALOG_TEXT:
                raise untrusted
        except BaseException:
            # Also on cancellation; shielded so the close itself isn't cancelled, and a
            # failing close must not replace the reason the start failed.
            try:
                await asyncio.shield(self._json("workspace", "close", workspace_id))
            except Exception:
                pass
            raise
        return Started(started["agent"], notice is not None and shown == notice)

    async def _start_when_shell_ready(
        self, name: str, pane_id: str, timeout_ms: int, agent_args: Sequence[str]
    ) -> dict:
        # A fresh pane's shell is still starting up, and herdr refuses `agent start`
        # with agent_pane_busy until it considers the shell available; seen to take
        # several seconds with a heavy zsh config.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SHELL_READY_TIMEOUT_S
        while True:
            try:
                return await self._json(
                    "agent", "start", name, "--kind", "claude", "--pane", pane_id,
                    "--timeout", str(timeout_ms), "--", *agent_args,
                )
            except HerdrError as error:
                if error.code != "agent_pane_busy" or loop.time() >= deadline:
                    raise
            await asyncio.sleep(SHELL_READY_POLL_S)

    async def _on_screen(self, pane_id: str, *texts: str | None) -> str | None:
        """The first of `texts` that shows up on the pane's screen within 5 s, else None.
        The trust dialog comes first, so it wins when both are on screen. Each text must be
        plain words: several are matched as one regular expression."""
        wanted = [text for text in texts if text]
        match = ("--match", wanted[0]) if len(wanted) == 1 else ("--regex", "|".join(wanted))
        try:
            result = await self._json(
                "pane", "wait-output", pane_id, "--source", "visible", *match, "--timeout", "5000",
            )
        except HerdrError as error:
            if error.code == "timeout":
                return None
            raise
        screen = f"{result.get('matched_line') or ''}\n{(result.get('read') or {}).get('text') or ''}"
        # A match herdr doesn't describe is taken as the first text, as before notices.
        return next((text for text in wanted if text in screen), wanted[0])

    async def close_pane(self, pane_id: str) -> None:
        await self._json("pane", "close", pane_id)


def _parse_error(stdout: str) -> HerdrError | None:
    try:
        error = json.loads(stdout)["error"]
    except (ValueError, KeyError, TypeError):
        return None
    return HerdrError(error["code"], error["message"])
