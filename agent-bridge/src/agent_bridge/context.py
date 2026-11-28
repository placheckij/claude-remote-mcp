"""How full each agent's context window is, as Claude Code itself reports it.

Claude Code hands its statusLine command the context window it works from (the numbers /context
shows). Setup wraps the statusLine of each agents' config with `agent-bridge-statusline`
(install/statusline.py): the relay saves those numbers per conversation in
<config>/agent-bridge/context/<session id>.json, then runs the status line that was there, with
the same input, so it shows as before. The bridge reads that file for agent_details.

Standard library only: the relay runs on every status line render.
"""

import base64
import binascii
import contextlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path

RELAY = "agent-bridge-statusline"
# The relay's option that carries the status line it wraps, base64 so no shell quoting applies.
THEN = "--then"
CONTEXT_FOLDER = Path("agent-bridge") / "context"
# Claude Code's session ids are UUIDs; anything else never becomes a file name.
SESSION_ID = re.compile(r"[A-Za-z0-9-]{1,64}")


def encode(command: str) -> str:
    return base64.urlsafe_b64encode(command.encode()).decode()


def decode(value: str) -> str | None:
    try:
        return base64.urlsafe_b64decode(value.encode()).decode()
    except (binascii.Error, UnicodeError, ValueError):
        return None


def config_dir() -> Path:
    """The Claude config the status line runs for: Claude Code passes its environment on."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def context_file(config: Path, session_id: str) -> Path | None:
    return config / CONTEXT_FOLDER / f"{session_id}.json" if SESSION_ID.fullmatch(session_id) else None


def save(data: bytes, config: Path) -> None:
    """Keep the session's context_window from one status line input. Every render writes, so a
    window Claude Code can't measure yet (after /compact) replaces numbers that no longer hold."""
    status = json.loads(data)
    session_id = status.get("session_id") if isinstance(status, dict) else None
    path = context_file(config, session_id) if isinstance(session_id, str) else None
    if path is None:
        return
    saved = {"context_window": status.get("context_window"), "model": (status.get("model") or {}).get("id")}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(saved, file)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def shell(command: str) -> list[str]:
    """How Claude Code would run `command`: Git Bash on Windows when it is installed, else
    PowerShell; a POSIX shell elsewhere."""
    if sys.platform != "win32":
        return [shutil.which("bash") or "/bin/sh", "-c", command]
    bash = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if not bash and (git := shutil.which("git")):
        # <Git>\cmd\git.exe; not System32's bash.exe, which is WSL's.
        bash = str(Path(git).resolve().parent.parent / "bin" / "bash.exe")
    if bash and Path(bash).is_file():
        return [bash, "-c", command]
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]


def main(argv: Sequence[str] | None = None) -> int:
    """agent-bridge-statusline [--then <base64 command>]: save the context window, then run the
    wrapped status line with the same input. Saving never fails the status line."""
    args = list(sys.argv[1:] if argv is None else argv)
    data = sys.stdin.buffer.read()
    with contextlib.suppress(Exception):
        save(data, config_dir())
    command = decode(args[1]) if len(args) == 2 and args[0] == THEN else None
    if not command:
        return 0
    sys.stdout.flush()
    try:
        return subprocess.run(shell(command), input=data).returncode
    except OSError:
        return 1


def context_use(saved: object) -> tuple[int, int, int] | None:
    """(percent used, tokens in the window, window size) from a saved context_window, or None
    when Claude Code couldn't measure it (early in a session, right after /compact)."""
    window = saved.get("context_window") if isinstance(saved, dict) else None
    if not isinstance(window, dict):
        return None
    size = _count(window.get("context_window_size"))
    usage = window.get("current_usage")
    tokens = None
    if isinstance(usage, dict):
        parts = [usage.get(key) for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
        if all(_count(part) is not None for part in parts):
            tokens = sum(int(part) for part in parts)
    if not tokens:
        tokens = _count(window.get("total_input_tokens"))
    if not size or not tokens:
        return None
    percent = window.get("used_percentage")
    if _count(percent) is None:
        percent = tokens * 100 / size
    return min(math.floor(percent), 100), tokens, size


def _count(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0 or not math.isfinite(value):
        return None
    return value


def read_context(session_id: str | None, configs: Iterable[Path]) -> tuple[int, int, int] | None:
    """The context use saved for `session_id` in any of `configs`; the newest when several have it."""
    if not session_id:
        return None
    found: list[tuple[float, Path]] = []
    for config in configs:
        path = context_file(config, session_id)
        with contextlib.suppress(OSError):
            if path is not None:
                found.append((path.stat().st_mtime, path))
    for _, path in sorted(found, reverse=True):
        try:
            return context_use(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return None
