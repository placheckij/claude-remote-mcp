"""Mark a project folder as trusted in Claude Code's config before starting an agent.

Claude Code keeps folder trust in `<config dir>/.claude.json` under
`projects[<path>].hasTrustDialogAccepted`, and trust of a folder covers everything
inside it. Without it, a new agent sits on the trust dialog whose default answer is
"No, exit".

Guard rails: only the personal config dir is ever written (never the default
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


def _is_default_config(config_dir: Path) -> bool:
    home = Path.home().resolve()
    return config_dir == home or config_dir.is_relative_to(home / ".claude")


def trust_folder(config_dir: Path, folder: Path, trust_root: Path) -> bool:
    """Trust `folder` unless it (or a parent) already is. Returns True if it wrote."""
    config_dir = config_dir.expanduser().resolve()
    folder = folder.expanduser().resolve()
    trust_root = trust_root.expanduser().resolve()
    if _is_default_config(config_dir):
        raise TrustError(f"refusing to edit the default Claude config in {config_dir}")
    if not folder.is_relative_to(trust_root):
        raise TrustError(f"{folder} is outside the trust root {trust_root}")

    path = config_dir / CONFIG_FILE
    config = json.loads(path.read_text())
    projects = config.setdefault("projects", {})
    if any(
        projects.get(str(candidate), {}).get("hasTrustDialogAccepted")
        for candidate in (folder, *folder.parents)
    ):
        return False

    projects.setdefault(str(folder), {})["hasTrustDialogAccepted"] = True
    _replace_atomically(path, json.dumps(config, indent=2))
    return True


def _replace_atomically(path: Path, content: str) -> None:
    # Running Claude sessions rewrite this file too; write a sibling and rename so a
    # reader never sees a half-written config.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as file:
            file.write(content)
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
