"""The context relay in the agents' Claude configs' statusLine, as setup leaves it (context.py).

Setup puts `agent-bridge-statusline --then <the command that was there>` in each agents'
config's settings.json, never in the default config, which is yours to change; uninstall puts
the old command back. The relay path is written so both shells Claude Code may run it with
(a POSIX shell; Git Bash or PowerShell on Windows) read it as one word.
"""

import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

from agent_bridge.context import RELAY, THEN, decode, encode
from agent_bridge.trust import is_default_config

SETTINGS = "settings.json"
# What the relay's command looks like: its path as one word, quoted by shlex or not.
_NAME = rf"{re.escape(RELAY)}(?:\.exe)?"
WRAPPED = re.compile(
    rf"(?P<relay>'(?:[^']*/)?{_NAME}'|(?:[^'\s]*/)?{_NAME})(?: {THEN} (?P<then>[A-Za-z0-9_=-]+))?"
)
# A Windows path both PowerShell and Git Bash take unquoted.
PLAIN = re.compile(r"[A-Za-z0-9_./:~-]+")


def relay_word(relay: Path) -> str:
    """`relay` as the first word of a statusLine command. Raises ValueError for a Windows path
    neither shell would read as one word even in its short (8.3) form."""
    if sys.platform != "win32":
        return shlex.quote(str(relay))
    path = relay.as_posix()
    if not PLAIN.fullmatch(path):
        path = Path(_short_path(str(relay))).as_posix()
    if not PLAIN.fullmatch(path):
        raise ValueError(f"{relay} has characters a status line command can't hold unquoted")
    return path


def _short_path(path: str) -> str:
    import ctypes

    size = ctypes.windll.kernel32.GetShortPathNameW(path, None, 0)
    buffer = ctypes.create_unicode_buffer(size)
    ctypes.windll.kernel32.GetShortPathNameW(path, buffer, size)
    return buffer.value or path


def wrapped_command(word: str, current: str | None) -> str:
    return f"{word} {THEN} {encode(current)}" if current else word


def unwrapped(command: str) -> tuple[bool, str | None]:
    """(whether `command` is the relay's, the command it wraps)."""
    match = WRAPPED.fullmatch(command)
    if match is None:
        return False, None
    then = match.group("then")
    return True, decode(then) if then else None


def install(config_dir: Path, relay: Path) -> str:
    """Wrap `config_dir`'s statusLine with the relay. Returns "written" or "already". Raises
    OSError, or ValueError for a settings.json that isn't JSON or a statusLine it can't wrap."""
    if is_default_config(config_dir):
        raise ValueError(f"refusing to edit the default Claude config in {config_dir}")
    word = relay_word(relay)
    path, settings = _load(config_dir)
    status = settings.get("statusLine")
    if status is None:
        status = {"type": "command"}
    if not isinstance(status, dict) or status.get("type", "command") != "command":
        raise ValueError(f"{path} has a statusLine that isn't a command")
    current = status.get("command")
    if current is not None and not isinstance(current, str):
        raise ValueError(f"{path} has a statusLine command that isn't text")
    ours, inner = unwrapped(current) if current else (False, None)
    command = wrapped_command(word, inner if ours else current)
    if command == current:
        return "already"
    settings["statusLine"] = {**status, "type": "command", "command": command}
    _write(path, settings)
    return "written"


def remove(config_dir: Path) -> bool:
    """Put back the statusLine the relay wraps (none, if there wasn't one). Returns True if it
    wrote. Raises OSError, or ValueError for a settings.json that isn't JSON."""
    path, settings = _load(config_dir)
    status = settings.get("statusLine")
    command = status.get("command") if isinstance(status, dict) else None
    ours, inner = unwrapped(command) if isinstance(command, str) else (False, None)
    if not ours:
        return False
    if inner:
        settings["statusLine"] = {**status, "command": inner}
    else:
        del settings["statusLine"]
    _write(path, settings)
    return True


def _load(config_dir: Path) -> tuple[Path, dict]:
    # Resolved, so a symlinked file is updated where it lives instead of being replaced by a copy.
    path = (config_dir / SETTINGS).resolve()
    try:
        # UTF-8 as Claude Code writes it, whatever the system's code page.
        settings = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return path, {}
    except ValueError:
        raise ValueError(f"{path} is not valid JSON") from None
    if not isinstance(settings, dict):
        raise ValueError(f"{path} is not a JSON object")
    return path, settings


def _write(path: Path, settings: dict) -> None:
    # Running Claude sessions read this file; write a sibling and rename so a reader never sees
    # a half-written one.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
            file.write(json.dumps(settings, indent=2) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
