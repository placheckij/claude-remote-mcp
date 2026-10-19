import json
from pathlib import Path

import pytest

from agent_bridge.trust import TrustError, trust_folder


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
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


def projects(config_dir: Path) -> dict:
    return json.loads((config_dir / ".claude.json").read_text())["projects"]


def test_trusts_exactly_the_folder_and_keeps_other_settings(
    config_dir: Path, trust_root: Path
) -> None:
    assert trust_folder(config_dir, trust_root / "app", trust_root) is True
    config = json.loads((config_dir / ".claude.json").read_text())
    assert config["userID"] == "u"
    assert config["projects"] == {str(trust_root / "app"): {"hasTrustDialogAccepted": True}}
    assert (config_dir / ".claude.json").stat().st_mode & 0o777 == 0o600


def test_skips_when_a_parent_is_already_trusted(config_dir: Path, trust_root: Path) -> None:
    trust_folder(config_dir, trust_root / "app", trust_root)
    (trust_root / "app" / "sub").mkdir()
    assert trust_folder(config_dir, trust_root / "app" / "sub", trust_root) is False
    assert str(trust_root / "app" / "sub") not in projects(config_dir)


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
