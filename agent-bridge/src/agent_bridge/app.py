"""Private HTTP API that mcp-gateway calls; every route maps onto herdr CLI calls.

Layout: routes.py (HTTP surface) → agents.py (what each call does) → herdr.py (the CLI).
schemas.py, errors.py and auth.py hold the API's bodies, error envelope and token check.
"""

from fastapi import Depends, FastAPI

from agent_bridge.agents import AgentService
from agent_bridge.auth import require_token
from agent_bridge.config import Settings
from agent_bridge.errors import install_error_handlers
from agent_bridge.herdr import Herdr
from agent_bridge.routes import router


def create_app(settings: Settings, herdr: Herdr | None = None) -> FastAPI:
    app = FastAPI(title="agent-bridge", dependencies=[Depends(require_token(settings.token))])
    app.state.agents = AgentService(settings, herdr or Herdr(binary=settings.herdr_bin))
    install_error_handlers(app)
    app.include_router(router)
    return app


def main() -> FastAPI:
    """uvicorn factory: `uvicorn agent_bridge.app:main --factory`."""
    return create_app(Settings.from_env())
