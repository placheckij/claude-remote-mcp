"""Private HTTP API that mcp-gateway calls; every route maps onto herdr CLI calls.

Layout: routes.py (HTTP surface) → agents.py (what each call does) → herdr.py (the CLI).
schemas.py, errors.py and auth.py hold the API's bodies, error envelope and token check.
cli.py runs it as `agent-bridge serve` and enrolls it with a gateway (gateway.py), keeping
what enrolling gives in the state file (state.py).
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Depends, FastAPI

from agent_bridge.agents import AgentService
from agent_bridge.auth import require_token
from agent_bridge.config import Settings
from agent_bridge.errors import install_error_handlers
from agent_bridge.gateway import keep_address
from agent_bridge.herdr import Herdr
from agent_bridge.routes import router
from agent_bridge.state import State, load_or_create, state_file


def create_app(
    settings: Settings, herdr: Herdr | None = None, state: State | None = None, state_path: Path | None = None
) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # In the background: serving never waits for the gateway, which may be down.
        report = None
        if state is not None and state.gateway is not None and settings.url is not None:
            report = asyncio.create_task(keep_address(state, settings, state_path))
        yield
        if report is not None:
            report.cancel()

    # No doc routes: FastAPI serves them outside app-level dependencies, i.e. without the token.
    app = FastAPI(
        title="agent-bridge",
        dependencies=[Depends(require_token(settings.token))],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    bridge_id = state.bridge_id if state is not None else None
    app.state.agents = AgentService(settings, herdr or Herdr(binary=settings.herdr_bin), bridge_id)
    install_error_handlers(app)
    app.include_router(router)
    return app


def main() -> FastAPI:
    """uvicorn factory, `uvicorn agent_bridge.app:main --factory`, as installs before
    `agent-bridge serve` run it."""
    path = state_file()
    return create_app(Settings.from_env(), state=load_or_create(path), state_path=path)
