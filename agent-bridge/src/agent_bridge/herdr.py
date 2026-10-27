"""Thin async wrapper around the herdr CLI.

Herdr is the source of truth for agents; this module only shells out to it and
parses its JSON envelopes (`{"result": ...}` or `{"error": {"code", "message"}}`).
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

Runner = Callable[[Sequence[str]], Awaitable[tuple[int, str, str]]]

SHELL_READY_TIMEOUT_S = 15
SHELL_READY_POLL_S = 0.5
TRUST_DIALOG_TEXT = "Yes, I trust this folder"


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
        except FileNotFoundError:
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

    async def list_agents(self) -> list[dict]:
        return (await self._json("agent", "list"))["agents"]

    async def get_agent(self, target: str) -> dict:
        return (await self._json("agent", "get", target))["agent"]

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
        result = await self._json(
            "agent", "prompt", target, text, "--wait", "--timeout", str(timeout_ms)
        )
        return result["agent"]

    async def wait(self, target: str, timeout_ms: int, until: Sequence[str] = ()) -> dict:
        """Wait for one of `until`, or by default until the agent settles (idle, done, blocked)."""
        until_args = [arg for status in until for arg in ("--until", status)]
        result = await self._json("agent", "wait", target, *until_args, "--timeout", str(timeout_ms))
        return result["agent"]

    async def send_keys(self, target: str, keys: Sequence[str]) -> None:
        await self._json("agent", "send-keys", target, *keys)

    async def start_agent(
        self,
        name: str,
        cwd: str,
        env: dict[str, str],
        agent_args: Sequence[str],
        timeout_ms: int = 60_000,
    ) -> dict:
        """Create a workspace in `cwd` and start Claude Code in its root pane."""
        env_args = [arg for key, value in env.items() for arg in ("--env", f"{key}={value}")]
        created = await self._json(
            "workspace", "create", "--cwd", cwd, "--label", name, "--no-focus", *env_args
        )
        workspace_id = created["workspace"]["workspace_id"]
        pane_id = created["root_pane"]["pane_id"]
        try:
            started = await self._start_when_shell_ready(name, pane_id, timeout_ms, agent_args)
            # Herdr reports the agent ready while Claude's folder-trust dialog is on
            # screen, and that dialog defaults to "No, exit": a prompt sent now would
            # quit Claude. Refuse instead of leaving a trap.
            if await self._shows_trust_dialog(pane_id):
                raise HerdrError("folder_not_trusted", f"Claude Code does not trust {cwd}")
        except BaseException:
            # Also on cancellation; shielded so the close itself isn't cancelled, and a
            # failing close must not replace the reason the start failed.
            try:
                await asyncio.shield(self._json("workspace", "close", workspace_id))
            except Exception:
                pass
            raise
        return started["agent"]

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

    async def _shows_trust_dialog(self, pane_id: str) -> bool:
        try:
            await self._json(
                "pane", "wait-output", pane_id, "--source", "visible",
                "--match", TRUST_DIALOG_TEXT, "--timeout", "5000",
            )
        except HerdrError as error:
            if error.code == "timeout":
                return False
            raise
        return True

    async def close_pane(self, pane_id: str) -> None:
        await self._json("pane", "close", pane_id)


def _parse_error(stdout: str) -> HerdrError | None:
    try:
        error = json.loads(stdout)["error"]
    except (ValueError, KeyError, TypeError):
        return None
    return HerdrError(error["code"], error["message"])
