import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_bridge.app import create_app
from agent_bridge.config import Settings
from agent_bridge.herdr import Herdr

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def agent(pane_id: str = "w1:p1", status: str = "idle", cwd: str = "/tmp/project") -> dict:
    return {
        "agent": "claude",
        "agent_status": status,
        "cwd": cwd,
        "foreground_cwd": cwd,
        "name": "probe",
        "pane_id": pane_id,
        "terminal_title_stripped": "repo-1",
        "workspace_id": pane_id.split(":")[0],
    }


def ok(result: dict) -> tuple[int, str, str]:
    return 0, json.dumps({"id": "cli", "result": result}), ""


def err(code: str) -> tuple[int, str, str]:
    # Like the real CLI: exit 1 with the error envelope on stderr.
    return 1, "", json.dumps({"id": "cli", "error": {"code": code, "message": code}})


class FakeRunner:
    """Answers herdr invocations by matching their leading arguments."""

    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str, str] | list]):
        self.responses = responses
        self.calls: list[list[str]] = []

    async def __call__(self, argv: Sequence[str]) -> tuple[int, str, str]:
        args = list(argv[1:])
        self.calls.append(args)
        for prefix in sorted(self.responses, key=len, reverse=True):
            if tuple(args[: len(prefix)]) == prefix:
                response = self.responses[prefix]
                # A list is consumed in order, its last entry repeating.
                if isinstance(response, list):
                    return response.pop(0) if len(response) > 1 else response[0]
                return response
        raise AssertionError(f"unexpected herdr call: {args}")

    def called(self, *prefix: str) -> list[list[str]]:
        return [call for call in self.calls if tuple(call[: len(prefix)]) == prefix]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "project").mkdir()
    return tmp_path


def client(runner: FakeRunner, root: Path, **overrides) -> TestClient:
    settings = Settings(token=TOKEN, allowed_roots=(root.resolve(),), **overrides)
    return TestClient(create_app(settings, Herdr(runner=runner)))


def test_rejects_missing_and_wrong_token(root: Path) -> None:
    c = client(FakeRunner({}), root)
    assert c.get("/agents").status_code in (401, 403)
    assert c.get("/agents", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_lists_agents_as_summaries(root: Path) -> None:
    runner = FakeRunner({("agent", "list"): ok({"agents": [agent()]})})
    response = client(runner, root).get("/agents", headers=AUTH)
    assert response.json() == [
        {
            "id": "w1:p1",
            "name": "probe",
            "title": "repo-1",
            "status": "idle",
            "cwd": "/tmp/project",
            "workspace_id": "w1",
        }
    ]


def test_unknown_agent_is_404(root: Path) -> None:
    runner = FakeRunner({("agent", "get"): err("agent_not_found")})
    response = client(runner, root).get("/agents/w9:p1", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "agent_not_found"


def test_output_keeps_the_tail_when_capped(root: Path) -> None:
    runner = FakeRunner({("agent", "read"): (0, "a" * 50 + "END", "")})
    response = client(runner, root, max_output_chars=10).get("/agents/w1:p1/output", headers=AUTH)
    assert response.json() == {"text": "a" * 7 + "END", "truncated": True}


def test_prompt_waits_then_returns_status_and_output(root: Path) -> None:
    runner = FakeRunner(
        {
            ("agent", "prompt"): ok({"agent": agent(status="done")}),
            ("agent", "read"): (0, "OK\n", ""),
        }
    )
    response = client(runner, root).post(
        "/agents/w1:p1/prompt", headers=AUTH, json={"text": "hi", "timeout_s": 30}
    )
    body = response.json()
    assert body["agent"]["status"] == "done"
    assert body["output"]["text"] == "OK"
    assert body["timed_out"] is False
    assert runner.called("agent", "prompt")[0] == [
        "agent", "prompt", "w1:p1", "hi", "--wait", "--timeout", "30000",
    ]


def test_prompt_timeout_reports_still_working(root: Path) -> None:
    runner = FakeRunner(
        {
            ("agent", "prompt"): err("timeout"),
            ("agent", "get"): ok({"agent": agent(status="working")}),
            ("agent", "read"): (0, "thinking", ""),
        }
    )
    body = client(runner, root).post("/agents/w1:p1/prompt", headers=AUTH, json={"text": "hi"}).json()
    assert body["timed_out"] is True
    assert body["agent"]["status"] == "working"


def start_responses(trust_dialog: bool) -> dict:
    return {
        ("workspace", "create"): ok(
            {"workspace": {"workspace_id": "w5"}, "root_pane": {"pane_id": "w5:p1"}}
        ),
        ("pane", "wait-output"): ok({"matched_line": "Yes, I trust this folder"})
        if trust_dialog
        else err("timeout"),
        ("agent", "start"): ok({"agent": agent("w5:p1")}),
        ("workspace", "close"): ok({"type": "ok"}),
    }


def test_start_uses_personal_config_and_auto_mode(root: Path) -> None:
    runner = FakeRunner(start_responses(trust_dialog=False))
    response = client(runner, root).post(
        "/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "probe"}
    )
    assert response.status_code == 201
    assert response.json()["id"] == "w5:p1"
    create = runner.called("workspace", "create")[0]
    assert f"CLAUDE_CONFIG_DIR={Path.home() / '.claude-agents'}" in create
    assert runner.called("agent", "start")[0][-3:] == ["--", "--permission-mode", "auto"]
    assert not runner.called("workspace", "close")


def test_start_retries_while_the_new_shell_is_busy(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agent_bridge.herdr.SHELL_READY_POLL_S", 0)
    responses = start_responses(trust_dialog=False)
    responses[("agent", "start")] = [
        err("agent_pane_busy"),
        err("agent_pane_busy"),
        ok({"agent": agent("w5:p1")}),
    ]
    runner = FakeRunner(responses)
    response = client(runner, root).post(
        "/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "probe"}
    )
    assert response.status_code == 201
    assert len(runner.called("agent", "start")) == 3


def test_start_refuses_and_cleans_up_when_folder_untrusted(root: Path) -> None:
    runner = FakeRunner(start_responses(trust_dialog=True))
    response = client(runner, root).post(
        "/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "probe"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "folder_not_trusted"
    assert runner.called("workspace", "close")[0] == ["workspace", "close", "w5"]


@pytest.mark.parametrize("cwd", ["/etc", "{root}/../outside", "{root}/project/../../escape"])
def test_start_rejects_paths_outside_allowed_roots(root: Path, cwd: str) -> None:
    runner = FakeRunner({})
    response = client(runner, root).post(
        "/agents", headers=AUTH, json={"cwd": cwd.format(root=root), "name": "probe"}
    )
    assert response.status_code == 403
    assert runner.calls == []


def test_stop_closes_the_pane(root: Path) -> None:
    runner = FakeRunner({("pane", "close"): ok({"type": "ok"})})
    assert client(runner, root).delete("/agents/w1:p1", headers=AUTH).status_code == 204
    assert runner.calls == [["pane", "close", "w1:p1"]]
