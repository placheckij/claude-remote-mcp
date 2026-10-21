"""Bearer-token check in front of the whole MCP app, with redacted logging of rejections."""

import hashlib
import hmac
import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

# Headers whose values are logged as they are; any other value is replaced by its length,
# so a credential in an unexpected header never reaches the log.
SAFE_HEADERS = frozenset({
    "host", "user-agent", "accept", "accept-encoding", "accept-language", "content-type",
    "content-length", "origin", "referer", "connection", "cf-ray", "cf-visitor",
    "cf-ipcountry", "cf-connecting-ip", "x-forwarded-for", "x-forwarded-proto",
    "mcp-protocol-version", "mcp-session-id",
})
# Schemes short and well known enough to show; anything else may be the token itself.
KNOWN_SCHEMES = frozenset({"bearer", "basic", "digest", "token"})


class BearerAuth:
    """Rejects every request without the connector's token, except the health check.

    Each rejection is logged with the request's headers, secrets redacted, so a client
    that sends the token differently can be diagnosed without exposing the token.
    """

    def __init__(self, app: ASGIApp, token: str, open_paths: frozenset[str] = frozenset({"/healthz"})):
        self.app = app
        self.expected = token.encode()
        self.open_paths = open_paths

    def _rejection(self, header: bytes | None) -> str | None:
        """Why the Authorization header is refused, or None if it is accepted."""
        if header is None:
            return "no authorization header"
        # The scheme is case-insensitive (RFC 9110), and claude.ai may lowercase it;
        # the token itself must match exactly.
        scheme, _, provided = header.partition(b" ")
        if scheme.lower() != b"bearer":
            return "scheme is not Bearer"
        if not hmac.compare_digest(provided, self.expected):
            return "wrong token"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] not in self.open_paths:
            reason = self._rejection(dict(scope["headers"]).get(b"authorization"))
            if reason:
                log.warning(
                    "401 %s %s: %s; expected token %s; headers: %s",
                    scope["method"], scope["path"], reason,
                    _describe_token(self.expected), _describe_headers(scope["headers"]),
                )
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _describe_token(token: bytes) -> str:
    """Length and a short hash: enough to tell two tokens apart, useless to an attacker."""
    return f"<{len(token)} chars, sha256 {hashlib.sha256(token).hexdigest()[:8]}>"


def _describe_headers(headers: list[tuple[bytes, bytes]]) -> str:
    parts = []
    for raw_name, raw_value in headers:
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        if name in ("authorization", "proxy-authorization"):
            scheme, separator, token = value.partition(" ")
            if separator and scheme.lower() in KNOWN_SCHEMES:
                value = f"{scheme!r} + {_describe_token(token.encode('latin-1'))}"
            else:
                value = f"no known scheme, {_describe_token(raw_value)}"
        elif name not in SAFE_HEADERS:
            value = f"<redacted, {len(value)} chars>"
        parts.append(f"{name}={value}")
    return ", ".join(parts)
