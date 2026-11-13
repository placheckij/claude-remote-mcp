"""Request and response bodies of the API, and their conversion from herdr's agent records.

Responses are trimmed to what a voice-driven caller needs; raw terminal output is capped.
"""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, model_validator

# Prompts go to herdr as one command-line argument; Linux caps a single argument at
# 128 KiB, and 20 000 characters stay below it even at 4 bytes each.
MAX_PROMPT_CHARS = 20_000


def _no_nul(value: str) -> str:
    # The OS can't pass a NUL byte in a command-line argument; running herdr would fail.
    if "\x00" in value:
        raise ValueError("must not contain NUL characters")
    return value


ArgvText = Annotated[str, AfterValidator(_no_nul)]


class AgentSummary(BaseModel):
    id: str
    name: str | None
    title: str | None
    status: str
    cwd: str | None
    workspace_id: str
    # The question an idle or done agent ended its turn with, if any: it is waiting for
    # the user's answer, though herdr can't tell that from any other finished turn.
    question: str | None = None
    # `cwd` under the closest allowed root, e.g. "grafik/api" (agents.project_of); None at
    # a root itself or outside every root.
    project: str | None = None


class StartedAgent(AgentSummary):
    # The account (AGENT_BRIDGE_ACCOUNTS) it was started with; None for an unnamed one.
    account: str | None


class Output(BaseModel):
    text: str
    truncated: bool


class PromptRequest(BaseModel):
    # The length check comes first, so its error names characters, not "items".
    text: Annotated[str, Field(min_length=1, max_length=MAX_PROMPT_CHARS), AfterValidator(_no_nul)]
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
    # True when herdr typed the prompt but saw no reaction within its 5 s window
    # (agent_prompt_stalled): it may still run, so it must not be sent again.
    unconfirmed: bool = False


class KeysRequest(BaseModel):
    keys: list[ArgvText] = Field(min_length=1, max_length=20)


class StartRequest(BaseModel):
    # A folder name under an allowed root, or an absolute path; exactly one of this and
    # `cwd` is given.
    project: ArgvText | None = Field(default=None, min_length=1)
    # An absolute path, as gateways before `project` send it. Kept for one release.
    cwd: ArgvText | None = None
    # Any spoken name; it's turned into one herdr accepts (agents.herdr_name). By default
    # the folder's name.
    name: str | None = Field(default=None, min_length=1, max_length=200)
    # One of AGENT_BRIDGE_ACCOUNTS, any case; by default the one for the folder.
    account: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _one_folder(self) -> "StartRequest":
        if (self.project is None) == (self.cwd is None):
            raise ValueError("give either project or cwd")
        return self


class RenameRequest(BaseModel):
    # As in StartRequest.
    name: str = Field(min_length=1, max_length=200)


class HostInfo(BaseModel):
    # 1 is the API before /info; a gateway tells them apart by /info answering 404.
    api_version: int
    # From the state file; a gateway checks it when the bridge enrolls.
    bridge_id: str | None = None
    name: str
    platform: str
    bridge_version: str
    # None when herdr can't be run.
    herdr_version: str | None
    roots: list[str]
    # Where a bare project name was looked for before /projects: the first root.
    projects_root: str
    accounts: list[str]
    max_agents: int


class ProjectFolder(BaseModel):
    cwd: str


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
