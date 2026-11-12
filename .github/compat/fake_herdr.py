#!/usr/bin/env python3
"""Stand-in for the herdr CLI, for the mixed-versions check (check.py).

Answers the commands agent-bridge runs with canned JSON envelopes, keeps its agents in the
JSON file FAKE_HERDR_STATE, and appends each command line to FAKE_HERDR_LOG (one JSON list
per line), so the check can see what reached herdr. Standard library only.
"""

import json
import os
import sys

STATE = os.environ["FAKE_HERDR_STATE"]
LOG = os.environ["FAKE_HERDR_LOG"]


def ok(result: dict) -> int:
    print(json.dumps({"result": result}))
    return 0


def fail(code: str, message: str) -> int:
    # herdr writes its error envelope to stderr and exits non-zero.
    print(json.dumps({"error": {"code": code, "message": message}}), file=sys.stderr)
    return 1


def option(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def main(args: list[str]) -> int:
    with open(LOG, "a") as log:
        log.write(json.dumps(args) + "\n")
    with open(STATE) as file:
        state = json.load(file)
    agents = state["agents"]
    command = args[:2]

    if args == ["--version"]:
        print("herdr 0.9.5")
        return 0
    if command == ["agent", "list"]:
        return ok({"agents": agents})
    if command == ["agent", "get"]:
        found = [agent for agent in agents if agent["pane_id"] == args[2]]
        return ok({"agent": found[0]}) if found else fail("agent_not_found", f"no agent {args[2]}")
    if command == ["agent", "read"]:
        print("❯ ")
        return 0
    if command == ["workspace", "create"]:
        number = state["next_workspace"]
        state["next_workspace"] += 1
        workspace = {"workspace_id": f"w{number}", "pane_id": f"w{number}:p1", "cwd": option(args, "--cwd")}
        state["workspaces"].append(workspace)
        save(state)
        return ok({"workspace": {"workspace_id": workspace["workspace_id"]}, "root_pane": {"pane_id": workspace["pane_id"]}})
    if command == ["agent", "start"]:
        pane_id = option(args, "--pane")
        workspace = next(w for w in state["workspaces"] if w["pane_id"] == pane_id)
        agent = {
            "pane_id": pane_id,
            "workspace_id": workspace["workspace_id"],
            "name": args[2],
            "agent_status": "idle",
            "cwd": workspace["cwd"],
            "foreground_cwd": workspace["cwd"],
            "terminal_title_stripped": "claude",
        }
        agents.append(agent)
        save(state)
        return ok({"agent": agent})
    if command == ["pane", "wait-output"]:
        # The trust dialog never shows up.
        return fail("timeout", "no match")
    if command == ["workspace", "close"]:
        return ok({})
    return fail("unknown_command", f"the stand-in herdr doesn't know {' '.join(args)}")


def save(state: dict) -> None:
    with open(STATE, "w") as file:
        json.dump(state, file)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
