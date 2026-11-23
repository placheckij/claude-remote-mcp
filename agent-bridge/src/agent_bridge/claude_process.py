"""The Claude Code process in a herdr pane, and its environment."""

from pathlib import PureWindowsPath

import psutil


def claude_process(process_info: dict) -> dict | None:
    """The pane's Claude Code among herdr's foreground processes, if it has one. Found by
    name: its MCP servers run in the pane too."""
    processes = process_info.get("foreground_processes") if isinstance(process_info, dict) else None
    claudes = [
        process
        for process in (processes if isinstance(processes, list) else [])
        # A positive int pid only: psutil.Process(None) would be this bridge itself.
        if isinstance(process, dict) and type(process.get("pid")) is int and process["pid"] > 0
        and any(
            isinstance(name, str) and PureWindowsPath(name).name.lower().removesuffix(".exe") == "claude"
            for name in (process.get("name"), process.get("argv0"))
        )
    ]
    if not claudes:
        return None
    # herdr lists newer processes first, so a claude the agent's tools run comes before the
    # agent's own: that one is the group leader, else the oldest.
    leader = process_info.get("foreground_process_group_id")
    return next((process for process in claudes if process["pid"] == leader), claudes[-1])


def claude_environ(process_info: dict) -> dict[str, str] | None:
    """The environment of the pane's Claude Code, from herdr's process info, if it has one
    and the OS lets it be read."""
    agent = claude_process(process_info)
    if agent is None:
        return None
    try:
        return psutil.Process(agent["pid"]).environ()
    except (psutil.Error, OSError):
        return None
