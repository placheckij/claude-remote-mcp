"""HTTP routes; each one hands its request to the app's AgentService."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request

from agent_bridge.agents import AgentService
from agent_bridge.errors import BridgeError
from agent_bridge.schemas import (
    AgentSummary,
    HostInfo,
    KeysRequest,
    Output,
    ProjectFolder,
    PromptRequest,
    PromptResult,
    RenameRequest,
    StartedAgent,
    StartRequest,
    WaitRequest,
)

router = APIRouter()


def get_agents(request: Request) -> AgentService:
    return request.app.state.agents


Agents = Annotated[AgentService, Depends(get_agents)]
# herdr pane ids, e.g. "w1:p1" or "w1F:p1". Anything else, notably a leading "-" that herdr
# would read as an option, is refused before it reaches the CLI.
AgentId = Annotated[str, Path(pattern=r"^w[0-9A-Za-z]+:p[0-9A-Za-z]+$")]


@router.get("/info")
async def info(agents: Agents) -> HostInfo:
    return await agents.info()


@router.get("/projects/{name}")
async def find_project(name: str, agents: Agents) -> ProjectFolder:
    folder = agents.find_project(name)
    if folder is None:
        raise BridgeError(404, "project_not_found", f"There is no project named {name!r}.")
    return ProjectFolder(cwd=str(folder))


@router.get("/agents")
async def list_agents(agents: Agents) -> list[AgentSummary]:
    return await agents.list_agents()


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: AgentId, agents: Agents) -> AgentSummary:
    return await agents.get_agent(agent_id)


@router.get("/agents/{agent_id}/output")
async def read_output(
    agent_id: AgentId,
    agents: Agents,
    lines: Annotated[int, Query(ge=1, le=500)] = 40,
    source: Literal["visible", "recent", "recent-unwrapped"] = "recent",
) -> Output:
    return await agents.read_output(agent_id, lines, source)


@router.post("/agents/{agent_id}/prompt")
async def prompt(agent_id: AgentId, body: PromptRequest, agents: Agents) -> PromptResult:
    return await agents.prompt(agent_id, body.text, body.timeout_s, body.lines)


@router.post("/agents/{agent_id}/wait")
async def wait(agent_id: AgentId, body: WaitRequest, agents: Agents) -> PromptResult:
    return await agents.wait(agent_id, body.timeout_s, body.lines, body.expect_change)


@router.post("/agents/{agent_id}/keys", status_code=204)
async def send_keys(agent_id: AgentId, body: KeysRequest, agents: Agents) -> None:
    await agents.send_keys(agent_id, body.keys)


@router.post("/agents", status_code=201)
async def start_agent(body: StartRequest, agents: Agents) -> StartedAgent:
    return await agents.start_agent(body.name, body.account, project=body.project, cwd=body.cwd)


@router.patch("/agents/{agent_id}")
async def rename_agent(agent_id: AgentId, body: RenameRequest, agents: Agents) -> AgentSummary:
    return await agents.rename_agent(agent_id, body.name)


@router.delete("/agents/{agent_id}", status_code=204)
async def stop_agent(agent_id: AgentId, agents: Agents) -> None:
    await agents.stop_agent(agent_id)
