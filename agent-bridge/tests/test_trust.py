import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_bridge.trust import TrustError, trust_folder


@pytest.fixture
def home(tmp_path: Path, set_home: Callable[[Path], None]) -> Path:
    set_home(tmp_path)
    return tmp_path


@pytest.fixture
def config_dir(home: Path) -> Path:
    path = home / ".claude-agents"
    path.mkdir()
    (path / ".claude.json").write_text(json.dumps({"userID": "u", "projects": {}}))
    (path / ".claude.json").chmod(0o600)
    return path


@pytest.fixture
def trust_root(home: Path) -> Path:
    path = home / "projects"
    (path / "app").mkdir(parents=True)
    return path


def key(folder: Path) -> str:
    """The key Claude Code looks `folder` up by: its resolved path with "/" separators."""
    return folder.resolve().as_posix()


def projects(config_dir: Path) -> dict:
    return json.loads((config_dir / ".claude.json").read_text())["projects"]


def test_trusts_exactly_the_folder_and_keeps_other_settings(
    config_dir: Path, trust_root: Path
) -> None:
    assert trust_folder(config_dir, trust_root / "app", trust_root) is True
    config = json.loads((config_dir / ".claude.json").read_text())
    assert config["userID"] == "u"
    assert config["projects"] == {key(trust_root / "app"): {"hasTrustDialogAccepted": True}}
    if sys.platform != "win32":  # Windows keeps only a read-only flag
        assert (config_dir / ".claude.json").stat().st_mode & 0o777 == 0o600


def test_skips_when_a_parent_is_already_trusted(config_dir: Path, trust_root: Path) -> None:
    trust_folder(config_dir, trust_root / "app", trust_root)
    (trust_root / "app" / "sub").mkdir()
    assert trust_folder(config_dir, trust_root / "app" / "sub", trust_root) is False
    assert key(trust_root / "app" / "sub") not in projects(config_dir)


@pytest.mark.parametrize("folder", ["", "other", "projects-evil"])
def test_refuses_folders_outside_the_trust_root(
    config_dir: Path, trust_root: Path, home: Path, folder: str
) -> None:
    (home / folder).mkdir(parents=True, exist_ok=True)
    with pytest.raises(TrustError, match="outside the trust root"):
        trust_folder(config_dir, home / folder, trust_root)
    assert projects(config_dir) == {}


def test_refuses_parent_escape(config_dir: Path, trust_root: Path) -> None:
    with pytest.raises(TrustError, match="outside the trust root"):
        trust_folder(config_dir, trust_root / "app" / ".." / "..", trust_root)


@pytest.mark.parametrize("default_dir", ["", ".claude", ".claude/nested"])
def test_never_edits_the_default_config(home: Path, trust_root: Path, default_dir: str) -> None:
    config_dir = home / default_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".claude.json").write_text("{}")
    with pytest.raises(TrustError, match="default Claude config"):
        trust_folder(config_dir, trust_root / "app", trust_root)
    assert (config_dir / ".claude.json").read_text() == "{}"


def other_case(path: Path) -> Path:
    """`path` spelled with its letters' case flipped; skips where the disk is case-sensitive."""
    flipped = Path(str(path).swapcase())
    if not flipped.exists():
        pytest.skip("case-sensitive file system")
    return flipped


@pytest.mark.parametrize("default_dir", ["", ".claude", ".claude/nested"])
def test_never_edits_the_default_config_spelled_differently(
    home: Path, trust_root: Path, default_dir: str
) -> None:
    # macOS disks are case-insensitive by default: /USERS/ME/.CLAUDE is ~/.claude.
    config_dir = home / default_dir
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".claude.json").write_text("{}")
    with pytest.raises(TrustError, match="default Claude config"):
        trust_folder(other_case(config_dir), trust_root / "app", trust_root)
    assert (config_dir / ".claude.json").read_text() == "{}"


def test_never_edits_a_config_file_linked_to_the_default(
    home: Path, config_dir: Path, trust_root: Path, symlink: Callable[[Path, Path], None]
) -> None:
    (home / ".claude.json").write_text("{}")
    (config_dir / ".claude.json").unlink()
    symlink(config_dir / ".claude.json", home / ".claude.json")
    with pytest.raises(TrustError, match="default Claude config"):
        trust_folder(config_dir, trust_root / "app", trust_root)
    assert (home / ".claude.json").read_text() == "{}"


def test_trust_root_spelled_differently_still_trusts_folders_inside(
    config_dir: Path, trust_root: Path
) -> None:
    assert trust_folder(config_dir, trust_root / "app", other_case(trust_root)) is True


def test_a_config_dir_without_a_login_is_refused_with_how_to_fix_it(
    home: Path, trust_root: Path
) -> None:
    fresh = home / ".claude-fresh"
    fresh.mkdir()
    with pytest.raises(TrustError, match=r"no Claude login yet.*CLAUDE_CONFIG_DIR="):
        trust_folder(fresh, trust_root / "app", trust_root)


def test_a_config_that_is_not_json_is_refused(config_dir: Path, trust_root: Path) -> None:
    (config_dir / ".claude.json").write_text("{not json")
    with pytest.raises(TrustError, match="not valid JSON"):
        trust_folder(config_dir, trust_root / "app", trust_root)
    assert (config_dir / ".claude.json").read_text() == "{not json"


@pytest.mark.parametrize("projects", [None, {"/elsewhere": None}])
def test_null_project_entries_are_tolerated(
    config_dir: Path, trust_root: Path, projects: dict | None
) -> None:
    (config_dir / ".claude.json").write_text(json.dumps({"projects": projects}))
    assert trust_folder(config_dir, trust_root / "app", trust_root) is True
    assert projects_of(config_dir)[key(trust_root / "app")] == {"hasTrustDialogAccepted": True}


def test_a_symlinked_config_file_is_updated_through_the_link(
    home: Path, config_dir: Path, trust_root: Path, symlink: Callable[[Path, Path], None]
) -> None:
    real = home / "dotfiles" / "claude.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"projects": {}}))
    (config_dir / ".claude.json").unlink()
    symlink(config_dir / ".claude.json", real)
    trust_folder(config_dir, trust_root / "app", trust_root)
    assert (config_dir / ".claude.json").is_symlink()
    assert key(trust_root / "app") in json.loads(real.read_text())["projects"]


def projects_of(config_dir: Path) -> dict:
    return json.loads((config_dir / ".claude.json").read_text())["projects"]


def test_keeps_non_ascii_paths_in_the_config_intact(config_dir: Path, trust_root: Path) -> None:
    # Read with the system's code page (cp1250 on a Polish Windows), "ż" came back garbled.
    other = {"/home/me/zażółć": {"hasTrustDialogAccepted": False}}
    (config_dir / ".claude.json").write_text(json.dumps({"projects": other}, ensure_ascii=False), encoding="utf-8")
    trust_folder(config_dir, trust_root / "app", trust_root)
    assert projects(config_dir)["/home/me/zażółć"] == {"hasTrustDialogAccepted": False}


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")


@windows_only
def test_writes_the_key_claude_code_reads_on_windows(config_dir: Path, trust_root: Path) -> None:
    # Claude Code ignores `C:\Users\me\app` and `c:/Users/me/app`: only `C:/Users/me/app`
    # (slashes, an upper-case drive, the folder's real casing) is honoured.
    trust_folder(config_dir, Path(str(trust_root / "app").lower()), trust_root)
    [written] = projects(config_dir)
    assert written == (trust_root / "app").resolve().as_posix()
    assert "\\" not in written and written[0].isupper() and written[1] == ":"


@windows_only
@pytest.mark.parametrize("spell", [str, lambda path: path.as_posix().lower(), lambda path: path.as_posix().upper()])
def test_a_parent_trusted_under_a_key_claude_code_ignores_is_not_trusted(
    config_dir: Path, trust_root: Path, spell: Callable[[Path], str]
) -> None:
    # `C:\…` (older bridges), `c:/…` (a session started from VS Code) or another casing:
    # Claude Code would still show the trust dialog, so the exact key is written beside it.
    other = {spell(trust_root.resolve()): {"hasTrustDialogAccepted": True}}
    (config_dir / ".claude.json").write_text(json.dumps({"projects": other}))
    assert trust_folder(config_dir, trust_root / "app", trust_root) is True
    assert projects(config_dir) == {**other, key(trust_root / "app"): {"hasTrustDialogAccepted": True}}


def test_a_parent_trusted_under_the_exact_key_is_enough(config_dir: Path, trust_root: Path) -> None:
    trusted = {key(trust_root): {"hasTrustDialogAccepted": True}}
    (config_dir / ".claude.json").write_text(json.dumps({"projects": trusted}))
    assert trust_folder(config_dir, trust_root / "app", trust_root) is False
    assert projects(config_dir) == trusted
