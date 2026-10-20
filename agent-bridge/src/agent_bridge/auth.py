"""Bearer-token check applied to every route."""

import hmac
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agent_bridge.errors import BridgeError


def require_token(token: str) -> Callable[..., None]:
    # auto_error=False so a missing header gets the same 401 envelope as a wrong token.
    bearer = HTTPBearer(auto_error=False)

    def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        presented = credentials.credentials if credentials else ""
        if not hmac.compare_digest(presented.encode(), token.encode()):
            raise BridgeError(401, "unauthorized", "invalid token")

    return authorize
