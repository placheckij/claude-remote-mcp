"""LAN-only HTTP API that mcp-gateway calls; every route maps onto herdr CLI calls.

Agents are addressed by their herdr pane id (e.g. `w1:p1`). Responses are trimmed to
what a voice-driven caller needs; raw terminal output is capped.
"""

import asyncio
import hmac
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from agent_bridge.config import Settings
from agent_bridge.herdr import Herdr, HerdrError
from agent_bridge.screen import SNAPSHOT_LINES, last_lines
from agent_bridge.trust import TrustError, trust_folder

# How long a wait with expect_change gives the agent to leave its old state; herdr's own
# `agent prompt --wait` uses the same window.
STATE_CHANGE_WINDOW_MS = 5000

ERROR_STATUS = {
    "agent_not_found": 404,
    "pane_not_found": 404,
    "folder_not_trusted": 409,
    "agent_prompt_stalled": 409,
    "timeout": 504,
}


class AgentSummary(BaseModel):
    id: str
    name: str | None
    title: str | None
    status: str
    cwd: str | None
    workspace_id: str


class Output(BaseModel):
    text: str
    truncated: bool


class PromptRequest(BaseModel):
    text: str = Field(min_length=1)
    timeout_s: int | None = Field(default=None, ge=1, le=3600)
    lines: int = Field(default=40, ge=1, le=500)


class WaitRequest(BaseModel):
    timeout_s: int | None = Field(default=None, ge=1, le=3600)
    lines: int = Field(default=40, ge=1, le=500)
    # Set right after answering the agent: its status is still the old one (e.g. blocked),
    # so first wait briefly for it to start working before waiting for it to settle.
    expect_change: bool = False


class PromptResult(BaseModel):
    agent: AgentSummary
    output: Output
    # True when the agent was still working at the timeout; poll /output or /agents/{id}.
    timed_out: bool = False


class KeysRequest(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=20)


class StartRequest(BaseModel):
    cwd: str
    name: str = Field(min_length=1, max_length=64, pattern=r"^[\w.-]+$")


def summarize(agent: dict) -> AgentSummary:
    return AgentSummary(
        id=agent["pane_id"],
        name=agent.get("name"),
        title=agent.get("terminal_title_stripped"),
        status=agent.get("agent_status", "unknown"),
        cwd=agent.get("foreground_cwd") or agent.get("cwd"),
        workspace_id=agent["workspace_id"],
    )


def cap(text: str, limit: int) -> Output:
    """Keep the tail: the end of the terminal is where the latest answer is."""
    if len(text) <= limit:
        return Output(text=text, truncated=False)
    return Output(text=text[-limit:], truncated=True)


def create_app(settings: Settings, herdr: Herdr | None = None) -> FastAPI:
    herdr = herdr or Herdr(binary=settings.herdr_bin)
    bearer = HTTPBearer()

    def authorize(credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]) -> None:
        if not hmac.compare_digest(credentials.credentials.encode(), settings.token.encode()):
            raise HTTPException(status_code=401, detail="invalid token")

    app = FastAPI(title="agent-bridge", dependencies=[Depends(authorize)])
    # Serializes count-then-start so concurrent requests cannot exceed the limit.
    start_lock = asyncio.Lock()

    @app.exception_handler(HerdrError)
    async def herdr_error(_: Request, error: HerdrError) -> JSONResponse:
        return JSONResponse(
            status_code=ERROR_STATUS.get(error.code, 502),
            content={"error": {"code": error.code, "message": error.message}},
        )

    @app.get("/agents")
    async def list_agents() -> list[AgentSummary]:
        return [summarize(agent) for agent in await herdr.list_agents()]

    @app.get("/agents/{agent_id}")
    async def get_agent(agent_id: str) -> AgentSummary:
        return summarize(await herdr.get_agent(agent_id))

    @app.get("/agents/{agent_id}/output")
    async def read_output(
        agent_id: str,
        lines: Annotated[int, Query(ge=1, le=500)] = 40,
        source: Literal["visible", "recent", "recent-unwrapped"] = "recent",
    ) -> Output:
        snapshot = await herdr.read(agent_id, SNAPSHOT_LINES, source)
        return cap(last_lines(snapshot, lines), settings.max_output_chars)

    async def settle(agent_id: str, waiting, lines: int) -> PromptResult:
        """Await `waiting` (a herdr wait), then report status and the latest output."""
        timed_out = False
        try:
            agent = await waiting
        except HerdrError as error:
            if error.code != "timeout":
                raise
            timed_out = True
            agent = await herdr.get_agent(agent_id)
        output = last_lines(await herdr.read(agent_id, SNAPSHOT_LINES), lines)
        return PromptResult(
            agent=summarize(agent),
            output=cap(output, settings.max_output_chars),
            timed_out=timed_out,
        )

    @app.post("/agents/{agent_id}/prompt")
    async def prompt(agent_id: str, body: PromptRequest) -> PromptResult:
        timeout_ms = body.timeout_s * 1000 if body.timeout_s else settings.prompt_timeout_ms
        return await settle(agent_id, herdr.prompt(agent_id, body.text, timeout_ms), body.lines)

    @app.post("/agents/{agent_id}/wait")
    async def wait(agent_id: str, body: WaitRequest) -> PromptResult:
        timeout_ms = body.timeout_s * 1000 if body.timeout_s else settings.prompt_timeout_ms

        async def waiting() -> dict:
            remaining = timeout_ms
            if body.expect_change:
                window = min(STATE_CHANGE_WINDOW_MS, timeout_ms)
                try:
                    await herdr.wait(agent_id, window, until=("working",))
                except HerdrError as error:
                    # No change seen: the turn may already be over, or the answer did not
                    # take; either way the settled wait below reports the current state.
                    if error.code != "timeout":
                        raise
                remaining -= window
            return await herdr.wait(agent_id, max(remaining, 1000))

        return await settle(agent_id, waiting(), body.lines)

    @app.post("/agents/{agent_id}/keys", status_code=204)
    async def send_keys(agent_id: str, body: KeysRequest) -> None:
        await herdr.send_keys(agent_id, body.keys)

    @app.post("/agents", status_code=201, response_model=AgentSummary)
    async def start_agent(body: StartRequest) -> AgentSummary | JSONResponse:
        cwd = Path(body.cwd).expanduser().resolve()
        if not settings.is_allowed(cwd):
            raise HTTPException(status_code=403, detail=f"{cwd} is outside the allowed roots")
        if not cwd.is_dir():
            raise HTTPException(status_code=404, detail=f"{cwd} is not a directory")
        async with start_lock:
            running = len(await herdr.list_agents())
            if running >= settings.max_agents:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": {
                            "code": "agent_limit_reached",
                            "message": (
                                f"Refusing to start a new agent: {running} agents are already "
                                f"running and the limit is {settings.max_agents}. "
                                "Stop an agent first."
                            ),
                        }
                    },
                )
            if cwd.is_relative_to(settings.trust_root.expanduser().resolve()):
                try:
                    await asyncio.to_thread(
                        trust_folder, settings.claude_config_dir, cwd, settings.trust_root
                    )
                except TrustError as error:
                    return JSONResponse(
                        status_code=409,
                        content={"error": {"code": "trust_refused", "message": str(error)}},
                    )
            agent = await herdr.start_agent(
                body.name, str(cwd), settings.claude_env(), settings.claude_args
            )
        return summarize(agent)

    @app.delete("/agents/{agent_id}", status_code=204)
    async def stop_agent(agent_id: str) -> None:
        await herdr.close_pane(agent_id)

    return app


def main() -> FastAPI:
    """uvicorn factory: `uvicorn agent_bridge.app:main --factory`."""
    return create_app(Settings.from_env())
