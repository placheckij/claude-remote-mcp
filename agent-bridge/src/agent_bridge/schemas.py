"""Request and response bodies of the API, and their conversion from herdr's agent records.

Responses are trimmed to what a voice-driven caller needs; raw terminal output is capped.
"""

from pydantic import BaseModel, Field


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
