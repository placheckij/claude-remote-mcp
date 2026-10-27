"""Plain-text replies built from agent-bridge's JSON.

They are read aloud in the car, so every reply is a few lines, with detail only on request.
"""


def describe(agent: dict) -> str:
    label = agent.get("name") or agent.get("title") or "unnamed"
    return f"{agent['id']} · {label} · {agent['status']} · {agent.get('cwd') or '?'}"


def report(result: dict, wait_seconds: int) -> str:
    """Status headline, the agent line, then its latest output."""
    agent = result["agent"]
    if result["timed_out"]:
        head = f"Still working after {wait_seconds}s. Keep waiting with wait_for_agent."
    elif agent["status"] == "blocked":
        head = "The agent is waiting for an approval. Ask the user, then use answer_agent."
    else:
        head = f"Agent is {agent['status']}."
    return f"{head}\n{describe(agent)}\n---\n{with_truncation_note(result['output'])}"


def with_truncation_note(output: dict) -> str:
    text = output["text"] or "(no output)"
    return f"(earlier output cut)\n{text}" if output["truncated"] else text


def invalid(errors: list[dict]) -> str:
    """Pydantic's (or FastAPI's) validation errors as one sentence, without the input."""
    return "; ".join(_problem(error) for error in errors) + "."


def _problem(error: dict) -> str:
    loc = [str(part) for part in error.get("loc", ())]
    if loc[:1] in (["body"], ["query"], ["path"]):  # FastAPI says where the field was
        loc = loc[1:]
    field = ".".join(loc) or "the request"
    kind, message = error.get("type"), str(error.get("msg", "is not valid"))
    if kind == "missing":
        return f"{field} is missing"
    if kind == "string_pattern_mismatch":
        if loc[-1:] == ["agent_id"]:
            return f'{field} must be an id from list_agents, like "w1:p1"'
        return f"{field} has the wrong form"
    if kind == "value_error":
        return f"{field} {message.removeprefix('Value error, ')}"
    return f"{field}: {message[:1].lower()}{message[1:]}"
