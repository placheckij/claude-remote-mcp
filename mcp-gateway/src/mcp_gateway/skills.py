"""The skills the gateway serves over MCP (the Skills extension), one folder each under
`skills/`: a `SKILL.md` with `name` and `description` frontmatter, and any files beside it."""

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any

from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.resources import TextResource
from mcp.server.skills import Skills
from mcp.shared.exceptions import MCPError
from mcp.shared.skills import (
    GetSkillParams,
    GetSkillResult,
    ListSkillsParams,
    ListSkillsResult,
    Skill,
    SkillResource,
)
from mcp.types import INVALID_PARAMS

SKILLS_DIR: Traversable = files("mcp_gateway") / "skills"


@dataclass(frozen=True)
class LoadedSkill:
    skill: Skill
    # Each file's bytes by its skill:// URI, served exactly as the manifest describes them.
    files: dict[str, bytes]


def load_skills(root: Traversable) -> list[LoadedSkill]:
    """Every skill folder under `root`, sorted by name; a folder that isn't a valid skill
    fails startup rather than serving a skill clients would refuse."""
    return [_load(folder) for folder in sorted(root.iterdir(), key=lambda f: f.name) if folder.is_dir()]


def _load(folder: Traversable) -> LoadedSkill:
    skill_md = folder / "SKILL.md"
    if not skill_md.is_file():
        raise ValueError(f"Skill folder {folder.name} has no SKILL.md.")
    frontmatter = _frontmatter(skill_md.read_bytes().decode("utf-8"))
    if frontmatter.get("name") != folder.name:
        raise ValueError(f"Skill folder {folder.name}: SKILL.md's name must be {folder.name!r}.")
    if not frontmatter.get("description"):
        raise ValueError(f"Skill folder {folder.name}: SKILL.md has no description.")
    contents = {f"skill://{folder.name}/{path}": data for path, data in _files(folder)}
    resources = [
        SkillResource(uri=uri, digest=f"sha256:{sha256(data).hexdigest()}", size=len(data))
        for uri, data in contents.items()
    ]
    skill = Skill(uri=f"skill://{folder.name}/SKILL.md", frontmatter=frontmatter, resources=resources)
    return LoadedSkill(skill, contents)


def _files(folder: Traversable, prefix: str = "") -> list[tuple[str, bytes]]:
    found = []
    for entry in sorted(folder.iterdir(), key=lambda f: f.name):
        if entry.is_dir():
            found += _files(entry, f"{prefix}{entry.name}/")
        else:
            found.append((f"{prefix}{entry.name}", entry.read_bytes()))
    return found


def _frontmatter(text: str) -> dict[str, str]:
    """The `key: value` lines between the opening and closing `---`."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---" or "---" not in [line.strip() for line in lines[1:]]:
        return {}
    end = [line.strip() for line in lines].index("---", 1)
    pairs = (line.split(":", 1) for line in lines[1:end] if ":" in line)
    return {key.strip(): value.strip() for key, value in pairs}


def register_skills(mcp: MCPServer, skills: list[LoadedSkill]) -> None:
    for loaded in skills:
        for uri, data in loaded.files.items():
            mime_type = "text/markdown" if uri.endswith(".md") else "text/plain"
            name = uri.rsplit("/", 1)[-1]
            mcp.add_resource(TextResource(uri=uri, name=name, mime_type=mime_type, text=data.decode("utf-8")))


def skills_extension(skills: list[LoadedSkill]) -> Skills:
    by_uri = {loaded.skill.uri: loaded.skill for loaded in skills}

    async def list_skills(ctx: ServerRequestContext[Any, Any], params: ListSkillsParams) -> ListSkillsResult:
        return ListSkillsResult(skills=list(by_uri.values()))

    async def get_skill(ctx: ServerRequestContext[Any, Any], params: GetSkillParams) -> GetSkillResult:
        if params.uri not in by_uri:
            raise MCPError(code=INVALID_PARAMS, message=f"Unknown skill: {params.uri}")
        return GetSkillResult(skill=by_uri[params.uri])

    return Skills(list_skills=list_skills, get_skill=get_skill)
