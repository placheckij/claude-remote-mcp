#!/usr/bin/env python3
"""Checks that mcp-gateway and agent-bridge from two checkouts work together.

Runs each pairing (gateway / bridge: new / new, new / old, old / new), each with
MCP_GATEWAY_PROJECTS_ROOT set and unset. The bridge runs against a stand-in herdr
(fake_herdr.py); the gateway takes a static token. Each run calls the MCP tools over
JSON-RPC and checks the replies and the herdr commands they led to. What a reply should
say follows from the bridge under test: one without /info can't look up project names.

    python3 .github/compat/check.py --new . --old ../old

Each checkout needs its .venv (`uv sync --all-packages --frozen`). Standard library only.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE_TOKEN = "b" * 32
GATEWAY_TOKEN = "g" * 32
START_TIMEOUT_S = 30
# Agents herdr already runs: one in a project, one outside every root.
OUTSIDE = "/elsewhere/tool"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--new", type=Path, required=True, help="checkout of this branch")
    parser.add_argument("--old", type=Path, required=True, help="checkout of the old side")
    args = parser.parse_args()
    sides = {"new": args.new.resolve(), "old": args.old.resolve()}
    failed = []
    for gateway, bridge in (("new", "new"), ("new", "old"), ("old", "new")):
        for root_set in (True, False):
            name = f"gateway {gateway} / bridge {bridge}, projects root {'set' if root_set else 'unset'}"
            print(f"\n== {name}", flush=True)
            if not Run(sides[gateway], sides[bridge], root_set, old_gateway=gateway == "old").check():
                failed.append(name)
    print()
    for name in failed:
        print(f"FAILED: {name}")
    print("All pairings passed." if not failed else f"{len(failed)} run(s) failed.")
    return 1 if failed else 0


class Skip(Exception):
    """The old gateway can't run in this setup, e.g. one that needs a projects root."""


class Run:
    def __init__(self, gateway: Path, bridge: Path, root_set: bool, old_gateway: bool):
        self.gateway_dir = gateway
        self.bridge_dir = bridge
        self.root_set = root_set
        # Only the old gateway may skip: this branch's must run in every setup.
        self.old_gateway = old_gateway
        self.failures = 0

    def check(self) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            # Resolved: the bridge reports resolved paths (/var is /private/var on macOS).
            self.tmp = Path(tmp).resolve()
            self.root = self.tmp / "projects"
            for folder in ("alpha/api", "beta"):
                (self.root / folder).mkdir(parents=True)
            self.herdr_log = self.tmp / "herdr.log"
            self.herdr_log.touch()
            self.write_herdr_state()
            self.processes: list[subprocess.Popen] = []
            try:
                self.start_bridge()
                self.start_gateway()
                self.run_checks()
            except Skip as reason:
                print(f"skipped: {reason}")
            except Exception as error:
                self.fail(f"{type(error).__name__}: {error}")
            finally:
                for process in self.processes:
                    process.terminate()
                for process in self.processes:
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        # Stuck finishing a request: kill it, so the logs still print.
                        process.kill()
                        process.wait()
                if self.failures:
                    for log in sorted(self.tmp.glob("*.out")):
                        print(f"--- {log.name}\n{log.read_text()}")
        return self.failures == 0

    # --- Processes ---------------------------------------------------------------------

    def write_herdr_state(self) -> None:
        agents = [
            agent("w1:p1", "alpha-api", "working", str(self.root / "alpha/api")),
            agent("w2:p1", "tool", "idle", OUTSIDE),
        ]
        state = {"agents": agents, "workspaces": [], "next_workspace": 3}
        (self.tmp / "herdr.json").write_text(json.dumps(state))

    def start_bridge(self) -> None:
        herdr = self.tmp / "herdr"
        shutil.copy(HERE / "fake_herdr.py", herdr)
        herdr.chmod(0o755)
        self.bridge_url = f"http://127.0.0.1:{free_port()}"
        env = {
            "AGENT_BRIDGE_TOKEN": BRIDGE_TOKEN,
            "AGENT_BRIDGE_ALLOWED_ROOTS": str(self.root),
            "AGENT_BRIDGE_HERDR_BIN": str(herdr),
            "FAKE_HERDR_STATE": str(self.tmp / "herdr.json"),
            "FAKE_HERDR_LOG": str(self.herdr_log),
        }
        process = self.serve("bridge", self.bridge_dir, "agent_bridge.app:main", self.bridge_url, env)
        self.wait_until_up("bridge", process, f"{self.bridge_url}/info", BRIDGE_TOKEN)

    def start_gateway(self) -> None:
        self.gateway_url = f"http://127.0.0.1:{free_port()}"
        env = {
            "MCP_GATEWAY_OAUTH_PASSPHRASE": "compat-check-passphrase",
            "MCP_GATEWAY_STATE_DIR": str(self.tmp / "gateway-state"),
            "MCP_GATEWAY_TOKEN": GATEWAY_TOKEN,
            "MCP_GATEWAY_BRIDGE_URL": self.bridge_url,
            "MCP_GATEWAY_BRIDGE_TOKEN": BRIDGE_TOKEN,
            "MCP_GATEWAY_PUBLIC_HOST": "compat.invalid",
        }
        if self.root_set:
            env["MCP_GATEWAY_PROJECTS_ROOT"] = str(self.root)
        process = self.serve("gateway", self.gateway_dir, "mcp_gateway.server:main", self.gateway_url, env)
        try:
            self.wait_until_up("gateway", process, f"{self.gateway_url}/healthz", None)
        except RuntimeError:
            output = (self.tmp / "gateway.out").read_text()
            if self.old_gateway and not self.root_set and "MCP_GATEWAY_PROJECTS_ROOT" in output:
                raise Skip("this gateway needs MCP_GATEWAY_PROJECTS_ROOT") from None
            raise

    def serve(self, name: str, checkout: Path, factory: str, url: str, env: dict) -> subprocess.Popen:
        # Run from the temporary folder, so neither side reads a .env from its checkout,
        # and with no AGENT_BRIDGE_* or MCP_GATEWAY_* from the caller's environment.
        inherited = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("AGENT_BRIDGE_", "MCP_GATEWAY_"))
        }
        uvicorn = checkout / ".venv" / "bin" / "uvicorn"
        port = url.rsplit(":", 1)[1]
        with open(self.tmp / f"{name}.out", "w") as output:
            process = subprocess.Popen(
                [str(uvicorn), factory, "--factory", "--host", "127.0.0.1", "--port", port],
                cwd=self.tmp,
                env={**inherited, **env},
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        # Stopped at the end of the run, even if it never came up.
        self.processes.append(process)
        return process

    def wait_until_up(self, name: str, process: subprocess.Popen, url: str, token: str | None) -> None:
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"the {name} exited with code {process.returncode}")
            try:
                http("GET", url, token)
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError(f"the {name} didn't answer within {START_TIMEOUT_S} s")

    # --- Checks ------------------------------------------------------------------------

    def run_checks(self) -> None:
        status, _ = http("GET", f"{self.bridge_url}/info", BRIDGE_TOKEN)
        has_info = status == 200
        # A bridge without /info can't look up names; the gateway's projects root does.
        names_resolve = has_info or self.root_set
        print(f"bridge has /info: {has_info}")

        error, text = self.call("list_agents")
        alpha_api = "alpha/api" if names_resolve else str(self.root / "alpha/api")
        self.expect("list_agents answers", not error, text)
        self.expect("list_agents counts both agents", text.startswith("2 agents"), text)
        self.expect("list_agents shows the project agent's folder", f"w1:p1 · alpha-api · working · {alpha_api}" in text, text)
        self.expect("list_agents shows the outside agent's path", f"w2:p1 · tool · idle · {OUTSIDE}" in text, text)

        alpha = self.root / "alpha"
        (error, text), commands = self.call_logged("start_agent", project="alpha")
        if names_resolve:
            self.expect("start_agent by name starts", not error and text.startswith("Started"), text)
            self.expect("start_agent by name reports the project", "· idle · alpha" in text, text)
            self.expect("start_agent by name starts in its folder", started_in(commands) == [str(alpha)], commands)
            self.expect("start_agent by name is named after it", started_as(commands) == ["alpha"], commands)
        else:
            self.expect("start_agent by name is refused", error and "absolute path" in text, text)
            self.expect("start_agent by name starts nothing", started_in(commands) == [], commands)

        beta = self.root / "beta"
        (error, text), commands = self.call_logged("start_agent", project=str(beta), name="Beta test")
        self.expect("start_agent by path starts", not error and text.startswith("Started"), text)
        self.expect("start_agent by path starts in it", started_in(commands) == [str(beta)], commands)
        self.expect("start_agent by path takes the name", started_as(commands) == ["beta-test"], commands)

        (error, text), commands = self.call_logged("start_agent", project="nope")
        self.expect("start_agent with a bad name is refused", error, text)
        self.expect("start_agent with a bad name starts nothing", started_in(commands) == [], commands)

    def call(self, tool: str, **arguments) -> tuple[bool, str]:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        status, reply = http("POST", f"{self.gateway_url}/mcp", GATEWAY_TOKEN, body)
        if status != 200:
            raise RuntimeError(f"{tool}: HTTP {status}: {reply}")
        result = json.loads(reply)["result"]
        return result.get("isError", False), result["content"][0]["text"]

    def call_logged(self, tool: str, **arguments) -> tuple[tuple[bool, str], list[list[str]]]:
        """The reply, and the herdr commands the call led to."""
        seen = len(self.herdr_log.read_text().splitlines())
        reply = self.call(tool, **arguments)
        lines = self.herdr_log.read_text().splitlines()[seen:]
        return reply, [json.loads(line) for line in lines]

    def expect(self, what: str, passed: bool, detail: object) -> None:
        if passed:
            print(f"ok    {what}")
        else:
            self.fail(f"{what}\n      got: {detail}")

    def fail(self, message: str) -> None:
        self.failures += 1
        print(f"FAIL  {message}")


def agent(pane_id: str, name: str, status: str, cwd: str) -> dict:
    return {
        "pane_id": pane_id,
        "workspace_id": pane_id.split(":")[0],
        "name": name,
        "agent_status": status,
        "cwd": cwd,
        "foreground_cwd": cwd,
        "terminal_title_stripped": "claude",
    }


def started_in(commands: list[list[str]]) -> list[str]:
    """The folders herdr was asked to create a workspace in."""
    return [c[c.index("--cwd") + 1] for c in commands if c[:2] == ["workspace", "create"]]


def started_as(commands: list[list[str]]) -> list[str]:
    """The names herdr was asked to start agents with."""
    return [c[2] for c in commands if c[:2] == ["agent", "start"]]


def http(method: str, url: str, token: str | None, body: dict | None = None) -> tuple[int, str]:
    """Status and body; raises OSError only when nothing answered."""
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


if __name__ == "__main__":
    sys.exit(main())
