"""Mark a project folder as trusted in Claude Code's config before starting an agent.

Claude Code keeps folder trust in `<config dir>/.claude.json` under
`projects[<path>].hasTrustDialogAccepted`, and trust of a folder covers everything
inside it. Without it, a new agent sits on the trust dialog whose default answer is
"No, exit".

Claude Code looks a folder up by the exact string of its working folder with "/"
separators: `/Users/me/app` on macOS, `C:/Users/me/app` on Windows, where a
`C:\\Users\\me\\app` or `c:/Users/me/app` key is ignored. Agents start in the resolved
folder, so the key is that folder's path with "/": its real casing and an upper-case
drive letter.

Guard rails: only a separate config dir (CLAUDE_CONFIG_DIR) is ever written (never the default
`~/.claude.json` or `~/.claude/`), and only the exact folder being started is trusted,
and only when it lies under the trust root.
"""

import json
import os
import tempfile
from pathlib import Path

CONFIG_FILE = ".claude.json"


class TrustError(Exception):
    pass


def is_default_config(config_dir: Path) -> bool:
    """True if `config_dir` is Claude Code's default config, however it is spelled.

    Folders are compared as files on disk, not as strings: on a case-insensitive disk (the
    macOS default) `/USERS/ME/.CLAUDE` is `~/.claude`, and a `.claude.json` linked to
    `~/.claude.json` would write the global config.
    """
    home = Path.home().resolve()
    config_dir = config_dir.expanduser().resolve()
    return (
        config_dir == home
        or config_dir.is_relative_to(home / ".claude")
        or _same_file(config_dir, home)
        or _is_inside(config_dir, home / ".claude")
        or _same_file(config_dir / CONFIG_FILE, home / CONFIG_FILE)
    )


def _is_inside(path: Path, folder: Path) -> bool:
    return any(_same_file(candidate, folder) for candidate in (path, *path.parents))


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:  # either one missing
        return False


def trust_folder(config_dir: Path, folder: Path, trust_root: Path) -> bool:
    """Trust `folder` unless it (or a parent) already is. Returns True if it wrote."""
    config_dir = config_dir.expanduser().resolve()
    folder = folder.expanduser().resolve()
    trust_root = trust_root.expanduser().resolve()
    if is_default_config(config_dir):
        raise TrustError(f"refusing to edit the default Claude config in {config_dir}")
    if not (folder.is_relative_to(trust_root) or _is_inside(folder, trust_root)):
        raise TrustError(f"{folder} is outside the trust root {trust_root}")

    # Resolved, so a symlinked file is updated where it lives instead of being replaced by
    # a copy (is_default_config has already refused a link to ~/.claude.json).
    path = (config_dir / CONFIG_FILE).resolve()
    try:
        # UTF-8 as Claude Code writes it, whatever the system's code page: read as cp1250
        # (Polish Windows), every non-ASCII path in the file would be garbled.
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TrustError(
            f"{config_dir} has no Claude login yet ({CONFIG_FILE} is missing). Run "
            f"CLAUDE_CONFIG_DIR={config_dir} claude once on the agent host and log in."
        ) from None
    except ValueError:
        raise TrustError(f"{path} is not valid JSON, so folder trust was not written") from None
    # Claude Code may leave null where an object is expected; treat it as empty.
    projects = config.get("projects") or {}
    config["projects"] = projects
    if _is_trusted(projects, folder):
        return False

    key = project_key(folder)
    projects[key] = {**(projects.get(key) or {}), "hasTrustDialogAccepted": True}
    _replace_atomically(path, json.dumps(config, indent=2))
    return True


def project_key(folder: Path) -> str:
    """The key Claude Code reads a resolved `folder`'s settings from."""
    return folder.as_posix()


def _is_trusted(projects: dict, folder: Path) -> bool:
    """True if `folder` or a parent is trusted, under any spelling of its key on Windows:
    Claude Code has written both `c:/…` and `C:/…` there, and older bridges `C:\\…`. This
    only decides whether to write; what is written is always the exact key."""
    trusted = {
        os.path.normcase(key)
        for key, entry in projects.items()
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted")
    }
    return any(os.path.normcase(candidate) in trusted for candidate in (folder, *folder.parents))


def _replace_atomically(path: Path, content: str) -> None:
    # Running Claude sessions rewrite this file too; write a sibling and rename so a
    # reader never sees a half-written config.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
            file.write(content)
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
