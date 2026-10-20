"""Every refusal the API makes is `{"error": {"code", "message"}}` with an HTTP status."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent_bridge.herdr import HerdrError

# herdr error codes that are the caller's doing; any other herdr failure is a 502.
HERDR_STATUS = {
    "agent_not_found": 404,
    "pane_not_found": 404,
    "folder_not_trusted": 409,
    "agent_prompt_stalled": 409,
    "timeout": 504,
}


class BridgeError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(BridgeError)
    async def bridge_error(_: Request, error: BridgeError) -> JSONResponse:
        return _response(error.status, error.code, error.message)

    @app.exception_handler(HerdrError)
    async def herdr_error(_: Request, error: HerdrError) -> JSONResponse:
        return _response(HERDR_STATUS.get(error.code, 502), error.code, error.message)


def _response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})
