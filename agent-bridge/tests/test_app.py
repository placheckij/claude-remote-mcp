import asyncio
import json
import re
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_bridge.agents import AgentService
from agent_bridge.app import create_app
from agent_bridge.config import Settings
from agent_bridge.errors import BridgeError
from agent_bridge.herdr import Herdr, subprocess_runner

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
    # Trust writes are covered in test_trust.py; keep these tests off the real config.
    overrides.setdefault("trust_root", root / "no-trust-writes")
    settings = Settings(token=TOKEN, allowed_roots=(root.resolve(),), **overrides)
    return TestClient(create_app(settings, Herdr(runner=runner)))


def test_rejects_missing_and_wrong_token(root: Path) -> None:
    c = client(FakeRunner({}), root)
    for headers in ({}, {"Authorization": "Bearer nope"}):
        response = c.get("/agents", headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_api_docs_are_not_served(root: Path, path: str) -> None:
    # FastAPI's doc routes skip app-level dependencies, so they would answer without a token.
    assert client(FakeRunner({}), root).get(path).status_code == 404


@pytest.mark.parametrize("agent_id", ["--help", "-h", "w1", "w1:p1;x", "p1:w1", "w1:p1:p2", "w 1:p1"])
def test_malformed_agent_ids_never_reach_herdr(root: Path, agent_id: str) -> None:
    # A leading "-" would reach herdr's argv as an option.
    runner = FakeRunner({})
    c = client(runner, root)
    for method, path in [("GET", ""), ("GET", "/output"), ("POST", "/keys"), ("DELETE", "")]:
        body = {"keys": ["1"]} if path == "/keys" else None
        response = c.request(method, f"/agents/{agent_id}{path}", headers=AUTH, json=body)
        assert response.status_code == 422, (method, path, response.text)
    assert runner.calls == []


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


def test_output_of_a_busy_agent_falls_back_to_the_visible_screen(root: Path) -> None:
    runner = FakeRunner(
        {
            ("agent", "read", "w1:p1", "--lines", "500", "--source", "recent"): err("agent_not_idle"),
            ("agent", "read", "w1:p1", "--lines", "500", "--source", "visible"): (0, "Allow rm?\n", ""),
        }
    )
    response = client(runner, root).get("/agents/w1:p1/output", headers=AUTH)
    assert response.json() == {"text": "Allow rm?", "truncated": False}


def test_visible_read_failures_are_not_retried(root: Path) -> None:
    runner = FakeRunner({("agent", "read"): err("agent_not_idle")})
    response = client(runner, root).get("/agents/w1:p1/output?source=visible", headers=AUTH)
    assert response.status_code == 502
    assert len(runner.called("agent", "read")) == 1


def test_prompt_waits_then_returns_status_and_output(root: Path) -> None:
    runner = FakeRunner(
        {
            ("agent", "get"): ok({"agent": agent(status="idle", cwd=str(root / "project"))}),
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


def test_prompt_to_a_blocked_agent_is_refused_without_typing(root: Path) -> None:
    # herdr types the text and presses Enter, which would pick the highlighted answer
    # (often "Yes") of the pending question.
    runner = FakeRunner(
        {("agent", "get"): ok({"agent": agent(status="blocked", cwd=str(root / "project"))})}
    )
    response = client(runner, root).post("/agents/w1:p1/prompt", headers=AUTH, json={"text": "hi"})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "agent_blocked"
    assert "answer_agent" in error["message"]
    assert runner.called("agent", "prompt") == []


def test_prompt_timeout_reports_still_working(root: Path) -> None:
    runner = FakeRunner(
        {
            ("agent", "prompt"): err("timeout"),
            # Idle when the prompt is sent, still working on it when the wait runs out.
            ("agent", "get"): [
                ok({"agent": agent(status="idle", cwd=str(root / "project"))}),
                ok({"agent": agent(status="working", cwd=str(root / "project"))}),
            ],
            ("agent", "read"): (0, "thinking", ""),
        }
    )
    body = client(runner, root).post("/agents/w1:p1/prompt", headers=AUTH, json={"text": "hi"}).json()
    assert body["timed_out"] is True
    assert body["agent"]["status"] == "working"


def test_wait_returns_once_the_agent_settles(root: Path) -> None:
    runner = FakeRunner(
        {
            ("agent", "wait"): ok({"agent": agent(status="done")}),
            ("agent", "read"): (0, "All tests pass\n", ""),
        }
    )
    body = client(runner, root).post(
        "/agents/w1:p1/wait", headers=AUTH, json={"timeout_s": 60, "lines": 5}
    ).json()
    assert (body["agent"]["status"], body["output"]["text"], body["timed_out"]) == (
        "done", "All tests pass", False,
    )
    # A plain wait matches herdr's default settled states, not "working".
    assert runner.called("agent", "wait") == [["agent", "wait", "w1:p1", "--timeout", "60000"]]


def test_wait_timeout_reports_still_working(root: Path) -> None:
    runner = FakeRunner(
        {
            ("agent", "wait"): err("timeout"),
            ("agent", "get"): ok({"agent": agent(status="working")}),
            ("agent", "read"): (0, "thinking", ""),
        }
    )
    body = client(runner, root).post("/agents/w1:p1/wait", headers=AUTH, json={}).json()
    assert body["timed_out"] is True
    assert body["agent"]["status"] == "working"


def test_wait_after_an_answer_skips_the_stale_blocked_state(root: Path) -> None:
    runner = FakeRunner(
        {
            ("agent", "wait", "w1:p1", "--until", "working"): ok({"agent": agent(status="working")}),
            ("agent", "wait", "w1:p1", "--timeout"): ok({"agent": agent(status="idle")}),
            ("agent", "read"): (0, "Done.", ""),
        }
    )
    body = client(runner, root).post(
        "/agents/w1:p1/wait", headers=AUTH, json={"timeout_s": 60, "expect_change": True}
    ).json()
    assert body["agent"]["status"] == "idle"
    assert runner.called("agent", "wait") == [
        ["agent", "wait", "w1:p1", "--until", "working", "--timeout", "5000"],
        ["agent", "wait", "w1:p1", "--timeout", "55000"],
    ]


def test_wait_after_an_answer_without_a_change_reports_current_state(root: Path) -> None:
    runner = FakeRunner(
        {
            ("agent", "wait", "w1:p1", "--until", "working"): err("timeout"),
            ("agent", "wait", "w1:p1", "--timeout"): ok({"agent": agent(status="blocked")}),
            ("agent", "read"): (0, "Allow rm?", ""),
        }
    )
    body = client(runner, root).post(
        "/agents/w1:p1/wait", headers=AUTH, json={"timeout_s": 30, "expect_change": True}
    ).json()
    assert (body["agent"]["status"], body["timed_out"]) == ("blocked", False)


def test_wait_for_unknown_agent_is_404(root: Path) -> None:
    runner = FakeRunner({("agent", "wait"): err("agent_not_found")})
    response = client(runner, root).post("/agents/w9:p9/wait", headers=AUTH, json={})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "agent_not_found"


@pytest.mark.parametrize("body", [{"timeout_s": 0}, {"timeout_s": 3601}, {"lines": 0}])
def test_wait_rejects_out_of_range_values(root: Path, body: dict) -> None:
    runner = FakeRunner({})
    assert client(runner, root).post("/agents/w1:p1/wait", headers=AUTH, json=body).status_code == 422
    assert runner.calls == []


def start_responses(trust_dialog: bool, running: int = 0) -> dict:
    return {
        ("agent", "list"): ok({"agents": [agent(f"w{i}:p1") for i in range(running)]}),
        ("workspace", "create"): ok(
            {"workspace": {"workspace_id": "w5"}, "root_pane": {"pane_id": "w5:p1"}}
        ),
        ("pane", "wait-output"): ok({"matched_line": "Yes, I trust this folder"})
        if trust_dialog
        else err("timeout"),
        ("agent", "start"): ok({"agent": agent("w5:p1")}),
        ("workspace", "close"): ok({"type": "ok"}),
    }


def test_start_uses_auto_mode_and_the_default_config(root: Path) -> None:
    runner = FakeRunner(start_responses(trust_dialog=False))
    response = client(runner, root).post(
        "/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "probe"}
    )
    assert response.status_code == 201
    assert response.json()["id"] == "w5:p1"
    create = runner.called("workspace", "create")[0]
    assert not any(arg.startswith("CLAUDE_CONFIG_DIR=") for arg in create)
    assert runner.called("agent", "start")[0][-3:] == ["--", "--permission-mode", "auto"]
    assert not runner.called("workspace", "close")


def test_start_passes_a_configured_claude_config_dir(root: Path) -> None:
    runner = FakeRunner(start_responses(trust_dialog=False))
    config_dir = root / ".claude-agents"
    response = client(runner, root, claude_config_dir=config_dir).post(
        "/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "probe"}
    )
    assert response.status_code == 201
    assert f"CLAUDE_CONFIG_DIR={config_dir}" in runner.called("workspace", "create")[0]


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
    response = client(runner, root, trust_root=None).post(
        "/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "probe"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "folder_not_trusted"
    assert runner.called("workspace", "close")[0] == ["workspace", "close", "w5"]
    message = response.json()["error"]["message"]
    assert message.startswith(f"Claude Code does not trust {(root / 'project').resolve()}")
    assert "AGENT_BRIDGE_TRUST_ROOT and AGENT_BRIDGE_CLAUDE_CONFIG_DIR" in message


@pytest.mark.parametrize("cwd", ["/etc", "{root}/../outside", "{root}/project/../../escape"])
def test_start_rejects_paths_outside_allowed_roots(root: Path, cwd: str) -> None:
    runner = FakeRunner({})
    response = client(runner, root).post(
        "/agents", headers=AUTH, json={"cwd": cwd.format(root=root), "name": "probe"}
    )
    assert response.status_code == 403
    assert runner.calls == []


def test_start_with_a_config_dir_that_has_no_login_explains_the_fix(root: Path) -> None:
    # A fresh AGENT_BRIDGE_CLAUDE_CONFIG_DIR has no .claude.json until Claude is logged in.
    (root / ".claude-fresh").mkdir()
    runner = FakeRunner({("agent", "list"): ok({"agents": []})})
    c = client(runner, root, claude_config_dir=root / ".claude-fresh", trust_root=root)
    response = c.post("/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "x"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "trust_refused"
    assert "no Claude login yet" in response.json()["error"]["message"]


async def missing_binary(argv) -> tuple[int, str, str]:
    raise FileNotFoundError(2, "No such file or directory", argv[0])


def test_a_missing_herdr_binary_is_reported_plainly(root: Path) -> None:
    c = TestClient(create_app(
        Settings(token=TOKEN, allowed_roots=(root.resolve(),), herdr_bin="/nowhere/herdr"),
        Herdr(binary="/nowhere/herdr", runner=missing_binary),
    ))
    response = c.get("/agents", headers=AUTH)
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "herdr_not_found"
    assert "AGENT_BRIDGE_HERDR_BIN" in error["message"]


def test_output_from_herdr_that_is_not_json_is_reported_plainly(root: Path) -> None:
    runner = FakeRunner({("agent", "list"): (0, "Usage: herdr agent list [OPTIONS]\n", "")})
    response = client(runner, root).get("/agents", headers=AUTH)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "bad_output"


CONTROL = [
    ("POST", "/agents/w1:p1/prompt", {"text": "hi"}, ("agent", "prompt")),
    ("POST", "/agents/w1:p1/keys", {"keys": ["1"]}, ("agent", "send-keys")),
    ("DELETE", "/agents/w1:p1", None, ("pane", "close")),
]


@pytest.mark.parametrize(("method", "path", "body", "herdr_call"), CONTROL)
def test_agents_outside_the_allowed_roots_can_be_read_but_not_controlled(
    root: Path, method: str, path: str, body: dict | None, herdr_call: tuple[str, ...]
) -> None:
    # Every herdr agent is listed, including ones in unrelated folders; a misheard id must
    # not prompt, answer or close those.
    runner = FakeRunner({("agent", "get"): ok({"agent": agent(cwd="/elsewhere/work-repo")})})
    c = client(runner, root)
    response = c.request(method, path, headers=AUTH, json=body)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "agent_not_allowed"
    assert runner.called(*herdr_call) == []
    assert c.get("/agents/w1:p1", headers=AUTH).status_code == 200


@pytest.mark.parametrize(("method", "path", "body", "herdr_call"), CONTROL[1:])
def test_agents_inside_the_allowed_roots_can_be_controlled(
    root: Path, method: str, path: str, body: dict | None, herdr_call: tuple[str, ...]
) -> None:
    runner = FakeRunner(
        {
            ("agent", "get"): ok({"agent": agent(cwd=str(root / "project"))}),
            herdr_call: ok({"type": "ok"}),
        }
    )
    response = client(runner, root).request(method, path, headers=AUTH, json=body)
    assert response.status_code == 204
    assert len(runner.called(*herdr_call)) == 1


def test_stop_refuses_panes_that_are_not_agents(root: Path) -> None:
    # A plain shell or dev-server pane has a pane id too.
    runner = FakeRunner({("agent", "get"): err("agent_not_found")})
    response = client(runner, root).delete("/agents/w1:p1", headers=AUTH)
    assert response.status_code == 404
    assert runner.called("pane", "close") == []


def test_stop_closes_the_pane(root: Path) -> None:
    runner = FakeRunner(
        {
            ("agent", "get"): ok({"agent": agent(cwd=str(root / "project"))}),
            ("pane", "close"): ok({"type": "ok"}),
        }
    )
    assert client(runner, root).delete("/agents/w1:p1", headers=AUTH).status_code == 204
    assert runner.called("pane", "close") == [["pane", "close", "w1:p1"]]


@pytest.mark.parametrize("running", [10, 12])
def test_start_refuses_at_the_agent_limit(root: Path, running: int) -> None:
    runner = FakeRunner(start_responses(trust_dialog=False, running=running))
    response = client(runner, root).post(
        "/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "probe"}
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "agent_limit_reached"
    assert error["message"] == (
        f"Refusing to start a new agent: {running} agents are already running "
        "and the limit is 10. Stop an agent first."
    )
    assert not runner.called("workspace", "create")


def test_start_allowed_just_below_the_limit(root: Path) -> None:
    runner = FakeRunner(start_responses(trust_dialog=False, running=9))
    response = client(runner, root).post(
        "/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "probe"}
    )
    assert response.status_code == 201


def test_start_trusts_folders_under_the_trust_root(root: Path) -> None:
    config_dir = root / "claude-config"
    config_dir.mkdir()
    (config_dir / ".claude.json").write_text("{}")
    runner = FakeRunner(start_responses(trust_dialog=False))
    response = client(runner, root, trust_root=root, claude_config_dir=config_dir).post(
        "/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "probe"}
    )
    assert response.status_code == 201
    trusted = json.loads((config_dir / ".claude.json").read_text())["projects"]
    assert trusted == {str((root / "project").resolve()): {"hasTrustDialogAccepted": True}}


def separate_home(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home folder next to `root`, not containing it: an allowed root may not be home."""
    home = root.parent / f"{root.name}-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_start_refuses_when_config_is_the_default_one(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = separate_home(root, monkeypatch)
    (home / ".claude").mkdir()
    (home / ".claude" / ".claude.json").write_text("{}")
    runner = FakeRunner(start_responses(trust_dialog=False))
    response = client(
        runner, root, trust_root=root, claude_config_dir=home / ".claude"
    ).post("/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "probe"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "trust_refused"
    assert not runner.called("workspace", "create")


def test_start_refuses_to_trust_without_a_separate_config(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No config dir means agents use the default config, which is never edited.
    home = separate_home(root, monkeypatch)
    (home / ".claude.json").write_text("{}")
    runner = FakeRunner(start_responses(trust_dialog=False))
    response = client(runner, root, trust_root=root).post(
        "/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "probe"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "trust_refused"
    assert (home / ".claude.json").read_text() == "{}"
    assert not runner.called("workspace", "create")


def test_start_without_a_trust_root_never_writes_trust(root: Path) -> None:
    config_dir = root / "claude-config"
    config_dir.mkdir()
    (config_dir / ".claude.json").write_text("{}")
    runner = FakeRunner(start_responses(trust_dialog=False))
    response = client(runner, root, trust_root=None, claude_config_dir=config_dir).post(
        "/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "probe"}
    )
    assert response.status_code == 201
    assert (config_dir / ".claude.json").read_text() == "{}"


# --- Starting agents anywhere else must fail ------------------------------------------


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    """A typical setup: ~/projects is both allowed and trusted, with a separate Claude config."""
    home = tmp_path
    projects_dir = home / "projects"
    (projects_dir / "app").mkdir(parents=True)
    (home / "other").mkdir()
    (home / "projects-evil").mkdir()
    (projects_dir / "link-out").symlink_to(home / "other")
    config_dir = home / ".claude-agents"
    config_dir.mkdir()
    (config_dir / ".claude.json").write_text('{"projects": {}}')
    return {"home": home, "projects_dir": projects_dir, "config_dir": config_dir}


def strict_client(runner: FakeRunner, layout: dict[str, Path], **overrides) -> TestClient:
    defaults = {
        "allowed_roots": (layout["projects_dir"].resolve(),),
        "trust_root": layout["projects_dir"],
        "claude_config_dir": layout["config_dir"],
    }
    settings = Settings(token=TOKEN, **{**defaults, **overrides})
    return TestClient(create_app(settings, Herdr(runner=runner)))


@pytest.mark.parametrize(
    "cwd",
    [
        "/",
        "/etc",
        "/tmp",
        "{home}",
        "{home}/other",
        "{home}/projects-evil",
        "{home}/projects/../other",
        "{home}/projects/link-out",
        "{home}/.claude-agents",
        "~",
    ],
)
def test_start_outside_allowed_roots_fails_without_side_effects(
    layout: dict[str, Path], cwd: str
) -> None:
    runner = FakeRunner({})
    response = strict_client(runner, layout).post(
        "/agents", headers=AUTH, json={"cwd": cwd.format(home=layout["home"]), "name": "probe"}
    )
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "path_not_allowed"
    assert "outside the allowed roots" in error["message"]
    assert runner.calls == []
    assert projects_in(layout["config_dir"]) == {}


def test_start_in_missing_folder_fails_without_side_effects(layout: dict[str, Path]) -> None:
    runner = FakeRunner({})
    response = strict_client(runner, layout).post(
        "/agents", headers=AUTH, json={"cwd": str(layout["projects_dir"] / "missing"), "name": "x"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_a_directory"
    assert runner.calls == []
    assert projects_in(layout["config_dir"]) == {}


def test_allowed_but_outside_trust_root_is_never_trusted(layout: dict[str, Path]) -> None:
    """With a broader allowlist, folders outside the trust root get no trust entry, and
    the trust dialog backstop refuses the start and closes the workspace."""
    runner = FakeRunner(start_responses(trust_dialog=True))
    client_ = strict_client(
        runner, layout, allowed_roots=(layout["home"].resolve(),)
    )
    response = client_.post(
        "/agents", headers=AUTH, json={"cwd": str(layout["home"] / "other"), "name": "x"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "folder_not_trusted"
    assert "outside the trust root" in response.json()["error"]["message"]
    assert runner.called("workspace", "close")
    assert projects_in(layout["config_dir"]) == {}


def test_rejects_empty_and_absurdly_long_names(layout: dict[str, Path]) -> None:
    # Anything else is turned into a name herdr accepts (see the tests below).
    runner = FakeRunner({})
    for name in ["", "x" * 201]:
        response = strict_client(runner, layout).post(
            "/agents", headers=AUTH, json={"cwd": str(layout["projects_dir"] / "app"), "name": name}
        )
        assert response.status_code == 422, name
    assert runner.calls == []


def projects_in(config_dir: Path) -> dict:
    return json.loads((config_dir / ".claude.json").read_text())["projects"]


@pytest.mark.parametrize(
    ("spoken", "herdr_name"),
    [
        ("Grafik fix!", "grafik-fix"), ("my.app", "my-app"), ("łódź", "lodz"), ("Żaba", "zaba"),
        ("1abc", "agent-1abc"), ("...", "agent"), ("x" * 40, "x" * 32), ("a b", "a-b"),
        ("../x", "x"), ("x;rm -rf", "x-rm-rf"), ("snake_case", "snake_case"),
    ],
)
def test_start_turns_any_name_into_one_herdr_accepts(root: Path, spoken: str, herdr_name: str) -> None:
    # herdr only takes [a-z][a-z0-9_-]{0,31}; anything else failed after the workspace was
    # created (and folder trust written).
    runner = FakeRunner(start_responses(trust_dialog=False))
    response = client(runner, root).post(
        "/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": spoken}
    )
    assert response.status_code == 201, response.text
    assert re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", herdr_name)
    assert runner.called("agent", "start")[0][2] == herdr_name
    created = runner.called("workspace", "create")[0]
    assert created[created.index("--label") + 1] == herdr_name


def test_start_picks_a_free_name_when_it_is_taken(root: Path) -> None:
    # herdr names must be unique among live agents.
    responses = start_responses(trust_dialog=False)
    responses[("agent", "list")] = ok(
        {"agents": [{**agent("w1:p1"), "name": "grafik"}, {**agent("w2:p1"), "name": "grafik-2"}]}
    )
    runner = FakeRunner(responses)
    response = client(runner, root).post(
        "/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "Grafik"}
    )
    assert response.status_code == 201
    assert runner.called("agent", "start")[0][2] == "grafik-3"


def test_prompt_to_a_working_agent_is_refused_without_typing(root: Path) -> None:
    # herdr's --wait can't tell turns apart: it would return the current turn's result as
    # the answer to this prompt.
    runner = FakeRunner(
        {("agent", "get"): ok({"agent": agent(status="working", cwd=str(root / "project"))})}
    )
    response = client(runner, root).post("/agents/w1:p1/prompt", headers=AUTH, json={"text": "hi"})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "agent_busy"
    assert "wait_for_agent" in error["message"]
    assert runner.called("agent", "prompt") == []


def test_a_stalled_prompt_is_reported_as_sent_but_unconfirmed(root: Path) -> None:
    # agent_prompt_stalled: the text was submitted but the agent showed no change within 5 s.
    # Calling that a failure would get the prompt sent twice.
    runner = FakeRunner(
        {
            ("agent", "get"): ok({"agent": agent(status="idle", cwd=str(root / "project"))}),
            ("agent", "prompt"): err("agent_prompt_stalled"),
            ("agent", "read"): (0, "> hi\n", ""),
        }
    )
    response = client(runner, root).post("/agents/w1:p1/prompt", headers=AUTH, json={"text": "hi"})
    assert response.status_code == 200
    body = response.json()
    assert body["unconfirmed"] is True and body["timed_out"] is False
    assert body["agent"]["status"] == "idle"
    assert body["output"]["text"] == "> hi"


def test_a_second_prompt_while_one_runs_is_refused(root: Path) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def runner(argv) -> tuple[int, str, str]:
        args = list(argv[1:])
        if args[:2] == ["agent", "get"]:
            return ok({"agent": agent(status="idle", cwd=str(root / "project"))})
        if args[:2] == ["agent", "prompt"]:
            started.set()
            await release.wait()
            return ok({"agent": agent(status="done")})
        if args[:2] == ["agent", "read"]:
            return 0, "OK\n", ""
        raise AssertionError(f"unexpected herdr call: {args}")

    settings = Settings(token=TOKEN, allowed_roots=(root.resolve(),), trust_root=root / "none")
    service = AgentService(settings, Herdr(runner=runner))

    async def scenario() -> None:
        first = asyncio.create_task(service.prompt("w1:p1", "one", 30, 5))
        await started.wait()
        try:
            # Without a guard the second prompt would wait behind the first, or be typed too.
            with pytest.raises(BridgeError) as refused:
                await asyncio.wait_for(service.prompt("w1:p1", "two", 30, 5), timeout=2)
            assert refused.value.code == "agent_busy"
        finally:
            release.set()
        assert (await first).agent.status == "done"

    asyncio.run(scenario())


def waits(runner: FakeRunner) -> list[int]:
    return [int(call[call.index("--timeout") + 1]) for call in runner.called("agent", "wait")]


def test_a_short_wait_after_an_answer_stays_within_its_budget(root: Path) -> None:
    # The 5 s state-change window used the whole budget; a further 1 s wait overran it.
    runner = FakeRunner(
        {
            ("agent", "wait"): err("timeout"),
            ("agent", "get"): ok({"agent": agent(status="blocked")}),
            ("agent", "read"): (0, "Allow?\n", ""),
        }
    )
    body = client(runner, root).post(
        "/agents/w1:p1/wait", headers=AUTH, json={"timeout_s": 5, "expect_change": True}
    ).json()
    assert sum(waits(runner)) <= 5000
    assert body["agent"]["status"] == "blocked"


def test_cancelling_a_start_still_closes_its_workspace(root: Path) -> None:
    started, closed = asyncio.Event(), []

    async def runner(argv) -> tuple[int, str, str]:
        args = list(argv[1:])
        if args[:2] == ["agent", "list"]:
            return ok({"agents": []})
        if args[:2] == ["workspace", "create"]:
            return ok({"workspace": {"workspace_id": "w5"}, "root_pane": {"pane_id": "w5:p1"}})
        if args[:2] == ["agent", "start"]:
            started.set()
            await asyncio.Event().wait()  # never answers
        if args[:2] == ["workspace", "close"]:
            closed.append(args[2])
            return ok({"type": "ok"})
        raise AssertionError(f"unexpected herdr call: {args}")

    settings = Settings(token=TOKEN, allowed_roots=(root.resolve(),), trust_root=root / "none")
    service = AgentService(settings, Herdr(runner=runner))

    async def scenario() -> None:
        task = asyncio.create_task(service.start_agent(str(root / "project"), "x"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert closed == ["w5"]


def test_a_failing_cleanup_does_not_hide_why_the_start_failed(root: Path) -> None:
    responses = start_responses(trust_dialog=False)
    responses[("agent", "start")] = err("agent_start_failed")
    responses[("workspace", "close")] = err("workspace_not_found")
    response = client(FakeRunner(responses), root).post(
        "/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "x"}
    )
    assert response.json()["error"]["code"] == "agent_start_failed"


def test_a_cancelled_herdr_call_kills_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    killed = []

    class Hanging:
        returncode = None

        async def communicate(self):
            await asyncio.Event().wait()

        def kill(self):
            killed.append(True)

        async def wait(self):
            return -9

    async def fake_exec(*argv, **kwargs):
        return Hanging()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def scenario() -> None:
        task = asyncio.create_task(subprocess_runner(["herdr", "agent", "list"]))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert killed == [True]


def not_detected() -> tuple[int, str, str]:
    # Seen once from herdr 0.8.0's `agent start` (not reproduced in 20 later tries): the
    # client found no agent in the pane when it checked.
    return 1, "", json.dumps(
        {"id": "cli", "error": {"code": "cli_failed", "message": "expected claude, detected "}}
    )


def test_a_start_where_claude_is_not_detected_is_retried_once(root: Path) -> None:
    responses = start_responses(trust_dialog=False)
    responses[("agent", "start")] = [not_detected(), ok({"agent": agent("w5:p1")})]
    runner = FakeRunner(responses)
    response = client(runner, root).post(
        "/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "x"}
    )
    assert response.status_code == 201
    assert len(runner.called("workspace", "create")) == 2
    assert len(runner.called("workspace", "close")) == 1  # the failed attempt's workspace


def test_a_start_where_claude_is_never_detected_is_explained(root: Path) -> None:
    responses = start_responses(trust_dialog=False)
    responses[("agent", "start")] = not_detected()
    runner = FakeRunner(responses)
    response = client(runner, root).post(
        "/agents", headers=AUTH, json={"cwd": str(root / "project"), "name": "x"}
    )
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "agent_not_detected"
    assert "try" in error["message"].lower()
    assert len(runner.called("workspace", "close")) == 2
