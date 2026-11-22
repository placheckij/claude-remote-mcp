"""herdr's own resume setting, and herdr's Claude hook, as setup leaves them.

agent-bridge brings Claude agents back itself after herdr's server restarts (restore.py), so
herdr's `[session] resume_agents_on_restore`, on by default, would bring each one back a second
time. Setup turns it off with a line it marks, and only when you haven't set it yourself;
`--no-agent-restore` and uninstall take that line out again. The file is edited as text, so the
rest of it stays byte for byte.

herdr's Claude hook (`herdr integration install claude`) tells herdr which conversation each
agent is in, which is how a restored agent resumes its own conversation rather than its
folder's latest one.
"""

import contextlib
import json
import os
import re
import stat
import sys
import tomllib
from pathlib import Path

from agent_bridge.install.common import PREFIX
from agent_bridge.trust import is_default_config

MARKER = "# agent-bridge: restores agents itself (setup --no-agent-restore undoes this)"
KEY_LINE = "resume_agents_on_restore = false"
# A [session] table header, with an optional comment; not [[session]] or [session.x].
SESSION_HEADER = re.compile(r"\s*\[\s*session\s*\]\s*(#.*)?")
# Any table header, which ends the one before it.
TABLE_HEADER = re.compile(r"\s*\[")
# What herdr's hook script is called, wherever it is.
HOOK_SCRIPT = "herdr-agent-state"


def herdr_config_paths() -> list[Path]:
    """The config files herdr may read: HERDR_CONFIG_PATH, else $XDG_CONFIG_HOME/herdr/config.toml,
    else ~/.config/herdr/config.toml (%APPDATA%\\herdr\\config.toml on Windows). herdr's server
    reads the one its own environment points at, e.g. a launch agent's or systemd unit's without
    XDG_CONFIG_HOME, which setup can't see: with XDG_CONFIG_HOME set elsewhere, both."""
    if path := os.environ.get("HERDR_CONFIG_PATH"):
        return [Path(path)]
    if sys.platform == "win32":
        return [Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "herdr" / "config.toml"]
    candidates = [Path.home() / ".config" / "herdr" / "config.toml"]
    # The XDG spec ignores a relative one.
    if (xdg := os.environ.get("XDG_CONFIG_HOME")) and Path(xdg).is_absolute():
        candidates.insert(0, Path(xdg) / "herdr" / "config.toml")
    paths: list[Path] = []
    for path in candidates:
        if not any(path.resolve() == other.resolve() for other in paths):
            paths.append(path)
    return paths


def default_claude_config() -> Path:
    """Claude Code's default config folder, which setup never writes to."""
    return Path.home() / ".claude"


def turn_off_resume(path: Path) -> str:
    """Sets herdr's resume_agents_on_restore to false unless it is set. Returns "written",
    "already" (our marked line is there) or "yours" (you set it yourself). Raises OSError, or
    ValueError for a file that isn't TOML, which is then left alone."""
    # Where it lives: a config linked from a dotfiles folder stays a link.
    path = path.resolve()
    text = _read(path)
    lines = text.splitlines(keepends=True)
    if _ours(lines) is not None:
        return "already"
    if "resume_agents_on_restore" in tomllib.loads(text).get("session", {}):
        return "yours"
    newline = "\r\n" if "\r\n" in text else "\n"
    ours = [f"{MARKER}{newline}", f"{KEY_LINE}{newline}"]
    header = next((index for index, line in enumerate(lines) if SESSION_HEADER.fullmatch(line.rstrip("\r\n"))), None)
    if header is not None:
        lines[header] = _ended(lines[header], newline)
        lines[header + 1:header + 1] = ours
    else:
        if lines:
            lines[-1] = _ended(lines[-1], newline)
            lines.append(newline)
        lines += [f"[session]{newline}", *ours]
    edited = "".join(lines)
    # Never a file herdr can't read, e.g. where [session] was an inline table already.
    if tomllib.loads(edited).get("session", {}).get("resume_agents_on_restore") is not False:
        raise ValueError("its [session] table couldn't be found to add the setting to")
    _write(path, edited)
    return "written"


def undo_resume(path: Path) -> bool:
    """Takes out the line turn_off_resume wrote, and the [session] header it appended when the
    table is empty then. A key line you have changed since is left. Returns whether it removed
    anything. Raises OSError."""
    path = path.resolve()
    if not path.exists():
        return False
    lines = _read(path).splitlines(keepends=True)
    index = _ours(lines)
    if index is None:
        return False
    del lines[index:index + 2]
    header = index - 1
    if header >= 0 and SESSION_HEADER.fullmatch(lines[header].rstrip("\r\n")) and _empty_table(lines[index:]):
        # The blank line it put before the table too.
        start = header - 1 if header > 0 and not lines[header - 1].strip() else header
        del lines[start:index]
    _write(path, "".join(lines))
    return True


def has_claude_hook(config_dir: Path) -> bool:
    """Whether `<config_dir>/settings.json` runs herdr's hook script at SessionStart. herdr's own
    `integration status` misses a hook that runs another config's copy of the script."""
    try:
        settings = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    entries = hooks.get("SessionStart") if isinstance(hooks, dict) else None
    for entry in entries if isinstance(entries, list) else ():
        for hook in (entry.get("hooks") or []) if isinstance(entry, dict) else ():
            if isinstance(hook, dict) and HOOK_SCRIPT in str(hook.get("command", "")):
                return True
    return False


def hook_config_dirs(values: dict[str, str]) -> list[Path]:
    """The Claude configs agents start in that setup gives herdr's hook: the agents' own and
    each account's, once each, never the default one, which is yours to change. Accounts that
    can't be read are warned about and skipped: agents still start without the hook."""
    found = [values.get(f"{PREFIX}CLAUDE_CONFIG_DIR") or ""]
    try:
        accounts = json.loads(values.get(f"{PREFIX}ACCOUNTS") or "{}")
        if not isinstance(accounts, dict) or not all(isinstance(account, dict) for account in accounts.values()):
            raise ValueError("not an object of accounts")
        found += [account.get("config_dir") or "" for account in accounts.values()]
    except ValueError as error:
        print(f"warning: {PREFIX}ACCOUNTS can't be read ({error}), so herdr's Claude hook isn't installed in the "
              "accounts' configs", file=sys.stderr)
    dirs: list[Path] = []
    for value in found:
        config_dir = Path(value).expanduser() if value else None
        if config_dir is None or is_default_config(config_dir):
            continue
        if not any(config_dir.resolve() == other.resolve() for other in dirs):
            dirs.append(config_dir)
    return dirs


def _read(path: Path) -> str:
    try:
        # TOML is UTF-8; bytes, so its line endings come back as they are.
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return ""


def _write(path: Path, text: str) -> None:
    """Written beside `path` and renamed over it, with its mode, so herdr never reads half of it."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o644
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        temporary.write_bytes(text.encode("utf-8"))
        temporary.chmod(mode)
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _ours(lines: list[str]) -> int | None:
    """Where our marker is, followed by our exact key line."""
    for index, line in enumerate(lines[:-1]):
        if line.rstrip("\r\n") == MARKER and lines[index + 1].rstrip("\r\n") == KEY_LINE:
            return index
    return None


def _empty_table(rest: list[str]) -> bool:
    """Whether the table these lines continue has nothing in it up to the next one."""
    for line in rest:
        if TABLE_HEADER.match(line):
            return True
        if line.strip():
            return False
    return True


def _ended(line: str, newline: str) -> str:
    return line if line.endswith(("\n", "\r")) else line + newline
