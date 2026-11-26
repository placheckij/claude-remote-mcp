from hashlib import sha256
from pathlib import Path

import pytest

from mcp_gateway.skills import SKILLS_DIR, load_skills


def skill_folder(root: Path, folder: str, frontmatter: str, extra: dict[str, str] | None = None) -> None:
    (root / folder).mkdir(parents=True)
    (root / folder / "SKILL.md").write_text(f"---\n{frontmatter}---\n\nBody.\n")
    for path, text in (extra or {}).items():
        (root / folder / path).parent.mkdir(parents=True, exist_ok=True)
        (root / folder / path).write_text(text)


def test_loads_a_skill_with_its_manifest(tmp_path: Path) -> None:
    skill_folder(tmp_path, "demo", "name: demo\ndescription: d\n", {"references/x.md": "x\n"})
    (loaded,) = load_skills(tmp_path)
    assert loaded.skill.uri == "skill://demo/SKILL.md"
    assert loaded.skill.frontmatter == {"name": "demo", "description": "d"}
    manifest = {resource.uri: resource for resource in loaded.skill.resources}
    assert set(manifest) == {"skill://demo/SKILL.md", "skill://demo/references/x.md"}
    for uri, resource in manifest.items():
        data = loaded.files[uri]
        assert resource.digest == f"sha256:{sha256(data).hexdigest()}"
        assert resource.size == len(data)


def test_refuses_a_name_that_differs_from_its_folder(tmp_path: Path) -> None:
    skill_folder(tmp_path, "demo", "name: other\ndescription: d\n")
    with pytest.raises(ValueError, match="demo"):
        load_skills(tmp_path)


def test_refuses_a_skill_without_a_description(tmp_path: Path) -> None:
    skill_folder(tmp_path, "demo", "name: demo\n")
    with pytest.raises(ValueError, match="demo"):
        load_skills(tmp_path)


def test_refuses_a_folder_without_skill_md(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    with pytest.raises(ValueError, match="demo"):
        load_skills(tmp_path)


def test_shipped_skill_is_within_limits() -> None:
    (loaded,) = load_skills(SKILLS_DIR)
    assert loaded.skill.frontmatter["name"] == "agent-teams"
    skill_md = loaded.files["skill://agent-teams/SKILL.md"]
    # Claude scans connector skills only up to 100 KB per text file.
    assert len(skill_md) < 100_000
    text = skill_md.decode()
    for needed in ("Owner:", "Depth:", "answer_agent", "stop_agent"):
        assert needed in text
