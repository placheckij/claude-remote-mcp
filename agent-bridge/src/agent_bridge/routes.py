"""HTTP routes; each one hands its request to the app's AgentService."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request

from agent_bridge.agents import AgentService
from agent_bridge.schemas import (
    AgentSummary,
    KeysRequest,
    Output,
    PromptRequest,
    PromptResult,
    StartRequest,
    WaitRequest,
)

router = APIRouter()


def get_agents(request: Request) -> AgentService:
    return request.app.state.agents


Agents = Annotated[AgentService, Depends(get_agents)]


@router.get("/agents")
async def list_agents(agents: Agents) -> list[AgentSummary]:
    return await agents.list_agents()


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, agents: Agents) -> AgentSummary:
    return await agents.get_agent(agent_id)


@router.get("/agents/{agent_id}/output")
async def read_output(
    agent_id: str,
    agents: Agents,
    lines: Annotated[int, Query(ge=1, le=500)] = 40,
    source: Literal["visible", "recent", "recent-unwrapped"] = "recent",
) -> Output:
    return await agents.read_output(agent_id, lines, source)


@router.post("/agents/{agent_id}/prompt")
async def prompt(agent_id: str, body: PromptRequest, agents: Agents) -> PromptResult:
    return await agents.prompt(agent_id, body.text, body.timeout_s, body.lines)


@router.post("/agents/{agent_id}/wait")
async def wait(agent_id: str, body: WaitRequest, agents: Agents) -> PromptResult:
    return await agents.wait(agent_id, body.timeout_s, body.lines, body.expect_change)


@router.post("/agents/{agent_id}/keys", status_code=204)
async def send_keys(agent_id: str, body: KeysRequest, agents: Agents) -> None:
    await agents.send_keys(agent_id, body.keys)


@router.post("/agents", status_code=201)
async def start_agent(body: StartRequest, agents: Agents) -> AgentSummary:
    return await agents.start_agent(body.cwd, body.name)


@router.delete("/agents/{agent_id}", status_code=204)
async def stop_agent(agent_id: str, agents: Agents) -> None:
    await agents.stop_agent(agent_id)
