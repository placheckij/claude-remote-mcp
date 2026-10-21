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
