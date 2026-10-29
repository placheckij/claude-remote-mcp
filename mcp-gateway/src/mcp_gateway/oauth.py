"""OAuth sign-in for the claude.ai connector: mcp-gateway is its own authorization server.

One user, one passphrase. claude.ai registers itself (dynamic client registration) and
sends the user to /authorize, which forwards to the sign-in page here. The right
passphrase becomes an authorization code for claude.ai's callback, and claude.ai trades it
for an access token (1 h) and a refresh token (90 days, replaced on every refresh).

The SDK's handlers check PKCE, code expiry, redirect URIs and which client owns a token;
this module keeps the records. Tokens are stored as SHA-256 hashes in
<state_dir>/oauth.json, so a copy of the file can't call the server, and a client is only
written there once it has signed in: registering is open to anyone.
"""

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp_gateway.config import Settings

log = logging.getLogger(__name__)

# The one scope there is: control of the agents.
SCOPE = "agents"
# The hosted Claude apps (web, desktop, mobile) all come back through this URL; no other
# may be registered, so a sign-in can't hand a code to anyone else.
REDIRECT_URIS = frozenset({"https://claude.ai/api/mcp/auth_callback"})
LOGIN_PATH = "/login"
ACCESS_TOKEN_TTL_S = 3600
REFRESH_TOKEN_TTL_S = 90 * 24 * 3600
CODE_TTL_S = 300
LOGIN_TTL_S = 600
# Registering and starting a sign-in need no credentials; this bounds what either can make
# the server hold in memory.
MAX_PENDING = 100
# Wrong passphrases: a sign-in link dies after this many,
MAX_TRIES_PER_SIGN_IN = 5
# and a source address that made this many within the window is refused until they age
# out. Per source, so a stranger can't lock the owner out by guessing.
MAX_FAILURES = 10
FAILURE_WINDOW_S = 900
# The client id that tokens accepted through the static MCP_GATEWAY_TOKEN carry.
STATIC_TOKEN_CLIENT = "static-token"


class GatewayOAuth:
    """The SDK's OAuthAuthorizationServerProvider, plus the sign-in page."""

    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time):
        self.passphrase = settings.oauth_passphrase.encode()
        self.static_token = settings.token.encode() if settings.token else None
        self.issuer = f"https://{settings.public_host}"
        # The connector URL exactly as entered in claude.ai; tokens are only for it.
        self.resource = f"{self.issuer}/mcp"
        self.resource_metadata_url = f"{self.issuer}/.well-known/oauth-protected-resource/mcp"
        self.clock = clock
        self.store = TokenStore(settings.state_dir / "oauth.json", clock)
        self._new_clients: OrderedDict[str, OAuthClientInformationFull] = OrderedDict()
        self._logins: OrderedDict[str, _SignIn] = OrderedDict()
        self._codes: OrderedDict[str, AuthorizationCode] = OrderedDict()
        # Source address -> times of its wrong passphrases.
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def auth_settings(self) -> AuthSettings:
        return AuthSettings(
            issuer_url=self.issuer,
            resource_server_url=self.resource,
            validate_token_resource=True,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        )

    # --- Clients ----------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.store.clients.get(client_id) or self._new_clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = {str(uri) for uri in client_info.redirect_uris or ()}
        if not uris or not uris <= REDIRECT_URIS:
            raise RegistrationError(
                "invalid_redirect_uri", f"Only {', '.join(sorted(REDIRECT_URIS))} can be registered."
            )
        _remember(self._new_clients, client_info.client_id, client_info)

    # --- Sign-in ----------------------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource is not None and params.resource.rstrip("/") != self.resource:
            raise AuthorizeError("invalid_target", f"This server only issues tokens for {self.resource}.")
        request_id = secrets.token_urlsafe(32)
        _remember(self._logins, request_id, _SignIn(client.client_id, params, self.clock() + LOGIN_TTL_S))
        return f"{self.issuer}{LOGIN_PATH}?request={request_id}"

    async def login(self, request: Request) -> Response:
        """The sign-in page (GET) and its form (POST)."""
        if request.method == "GET":
            request_id, passphrase = request.query_params.get("request", ""), None
        else:
            form = await request.form()
            request_id, passphrase = str(form.get("request", "")), str(form.get("passphrase", ""))
        sign_in = self._logins.get(request_id)
        client = await self.get_client(sign_in.client_id) if sign_in else None
        if not sign_in or not client or sign_in.expires_at < self.clock():
            self._logins.pop(request_id, None)
            return _page(
                "<p>This sign-in link has expired or was already used. Connect again from the "
                "Claude app.</p>",
                status=400,
            )
        params = sign_in.params
        if passphrase is None:
            return _sign_in_form(request_id, client, params)
        source = _source(request)
        if self._locked_out(source):
            log.warning(
                "OAuth sign-in refused for %s: %d wrong passphrases in %d s", source, MAX_FAILURES, FAILURE_WINDOW_S
            )
            return _page(
                f"<p>Too many wrong passphrases. Try again in {FAILURE_WINDOW_S // 60} minutes.</p>",
                status=429,
            )
        if not hmac.compare_digest(passphrase.encode(), self.passphrase):
            failures = self._failures.pop(source, deque())
            failures.append(self.clock())
            _remember(self._failures, source, failures)
            sign_in.wrong_tries += 1
            log.warning("OAuth sign-in: wrong passphrase from %s (client %r)", source, client.client_name)
            if sign_in.wrong_tries >= MAX_TRIES_PER_SIGN_IN:
                del self._logins[request_id]
                return _page(
                    "<p>Wrong passphrase too many times. Connect again from the Claude app.</p>",
                    status=403,
                )
            return _sign_in_form(request_id, client, params, error="Wrong passphrase.", status=403)
        del self._logins[request_id]
        code = AuthorizationCode(
            code=secrets.token_urlsafe(32),
            scopes=[SCOPE],
            expires_at=self.clock() + CODE_TTL_S,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self.resource,
        )
        _remember(self._codes, code.code, code)
        log.info("OAuth sign-in succeeded (client %r)", client.client_name)
        return RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=code.code, state=params.state),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    def _locked_out(self, source: str) -> bool:
        failures = self._failures.get(source, deque())
        while failures and failures[0] < self.clock() - FAILURE_WINDOW_S:
            failures.popleft()
        return len(failures) >= MAX_FAILURES

    # --- Tokens -----------------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self._codes.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Codes are good for one exchange.
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "authorization code was already used")
        return self.store.issue(client, authorization_code.scopes)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        record = self.store.find(refresh_token, "refresh")
        if record is None:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            resource=self.resource,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        return self.store.rotate(refresh_token.token, client, scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        # The static token is checked here too: the SDK takes one source of access tokens.
        if self.static_token and hmac.compare_digest(token.encode(), self.static_token):
            return AccessToken(token=token, client_id=STATIC_TOKEN_CLIENT, scopes=[SCOPE], resource=self.resource)
        record = self.store.find(token, "access")
        if record is None:
            return None
        return AccessToken(
            token=token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            resource=self.resource,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.revoke(token.token)


class TokenStore:
    """Signed-in clients and token hashes in one JSON file, rewritten whole on each change.

    Each sign-in starts a grant: the access and refresh token it issued, and those that
    replace them. Refreshing or revoking ends the grant's current tokens together.
    """

    def __init__(self, path: Path, clock: Callable[[], float]):
        self.path = path
        self.clock = clock
        self.clients: dict[str, OAuthClientInformationFull] = {}
        # sha256 of the token -> {kind, client_id, grant, scopes, expires_at}
        self.tokens: dict[str, dict] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
                self.clients = {
                    client_id: OAuthClientInformationFull.model_validate(client)
                    for client_id, client in data["clients"].items()
                }
                self.tokens = data["tokens"]
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise RuntimeError(
                    f"Can't read the OAuth state in {path} ({error}). Deleting it signs the "
                    "connector out; then connect it again in claude.ai."
                ) from None

    def find(self, token: str, kind: str) -> dict | None:
        record = self.tokens.get(_hash(token))
        if record is None or record["kind"] != kind or record["expires_at"] < self.clock():
            return None
        return record

    def issue(self, client: OAuthClientInformationFull, scopes: list[str], grant: str | None = None) -> OAuthToken:
        grant = grant or secrets.token_urlsafe(16)
        now = int(self.clock())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        for token, kind, ttl in ((access, "access", ACCESS_TOKEN_TTL_S), (refresh, "refresh", REFRESH_TOKEN_TTL_S)):
            self.tokens[_hash(token)] = {
                "kind": kind,
                "client_id": client.client_id,
                "grant": grant,
                "scopes": scopes,
                "expires_at": now + ttl,
            }
        self.clients[client.client_id] = client
        self._save()
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_TOKEN_TTL_S,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    def rotate(self, refresh_token: str, client: OAuthClientInformationFull, scopes: list[str]) -> OAuthToken:
        record = self.find(refresh_token, "refresh")
        if record is None:
            raise TokenError("invalid_grant", "refresh token does not exist")
        self._end(record["grant"])
        return self.issue(client, scopes, record["grant"])

    def revoke(self, token: str) -> None:
        record = self.tokens.get(_hash(token))
        if record is not None:
            self._end(record["grant"])
            self._save()

    def _end(self, grant: str) -> None:
        self.tokens = {key: record for key, record in self.tokens.items() if record["grant"] != grant}

    def _save(self) -> None:
        now = self.clock()
        self.tokens = {key: record for key, record in self.tokens.items() if record["expires_at"] >= now}
        # A client with no tokens left would have to sign in again anyway.
        signed_in = {record["client_id"] for record in self.tokens.values()}
        self.clients = {client_id: client for client_id, client in self.clients.items() if client_id in signed_in}
        data = {
            "clients": {
                client_id: client.model_dump(mode="json", exclude_none=True)
                for client_id, client in self.clients.items()
            },
            "tokens": self.tokens,
        }
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Written beside it and renamed over it, so a crash never leaves half a file.
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
            json.dump(data, file)
        os.replace(temporary, self.path)


@dataclass
class _SignIn:
    """A sign-in started at /authorize and not finished yet."""

    client_id: str
    params: AuthorizationParams
    expires_at: float
    wrong_tries: int = 0


def _source(request: Request) -> str:
    """Where a request comes from: the address Cloudflare saw, which it sets itself (a
    client can't forge it through the tunnel), or the peer address without a tunnel."""
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _remember(entries: OrderedDict, key: str, value) -> None:
    entries[key] = value
    while len(entries) > MAX_PENDING:
        entries.popitem(last=False)


def _sign_in_form(
    request_id: str,
    client: OAuthClientInformationFull,
    params: AuthorizationParams,
    error: str | None = None,
    status: int = 200,
) -> Response:
    # The MCP spec asks the consent page to show where the user is sent back to.
    destination = urlsplit(str(params.redirect_uri)).hostname or "?"
    name = html.escape(client.client_name or "An app")
    body = f"""
<p><strong>{name}</strong> wants to control the Claude Code agents on your agent host.
After you sign in, you go back to <strong>{html.escape(destination)}</strong>.</p>
{f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""}
<form method="post" action="{LOGIN_PATH}">
  <input type="hidden" name="request" value="{html.escape(request_id)}">
  <label for="passphrase">Passphrase</label>
  <input id="passphrase" name="passphrase" type="password" autocomplete="current-password" required autofocus>
  <button type="submit">Allow</button>
</form>
<p class="note">Only continue if you are connecting claude-remote in Claude right now.</p>"""
    # After the POST the browser follows a redirect to claude.ai; form-action must allow it.
    return _page(body, status, form_action=f"'self' https://{destination}")


def _page(body: str, status: int, form_action: str = "'self'") -> Response:
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>claude-remote sign-in</title>
<style>
  :root {{ color-scheme: light dark; --bg: #f6f5f2; --fg: #1d1c1a; --muted: #6b6862; --card: #fff;
           --line: #d9d6cf; --accent: #c2410c; --error: #b91c1c; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #191816; --fg: #ecebe8; --muted: #a29f98; --card: #24221f; --line: #3a3833;
             --accent: #f97316; --error: #f87171; }}
  }}
  body {{ margin: 0; background: var(--bg); color: var(--fg);
         font: 16px/1.5 system-ui, -apple-system, sans-serif; }}
  main {{ max-width: 26rem; margin: 12vh auto; padding: 1.5rem; background: var(--card);
         border: 1px solid var(--line); border-radius: 12px; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 1rem; }}
  label {{ display: block; font-weight: 600; margin: 1rem 0 .25rem; }}
  input {{ box-sizing: border-box; width: 100%; padding: .65rem .75rem; font: inherit;
          border: 1px solid var(--line); border-radius: 8px; background: var(--bg); color: var(--fg); }}
  button {{ margin-top: 1rem; width: 100%; padding: .7rem; font: inherit; font-weight: 600;
           border: 0; border-radius: 8px; background: var(--accent); color: #fff; }}
  .error {{ color: var(--error); font-weight: 600; }}
  .note {{ color: var(--muted); font-size: .875rem; margin-bottom: 0; }}
  @media (max-width: 30rem) {{ main {{ margin: 0; border: 0; border-radius: 0; min-height: 100vh; }} }}
</style>
</head>
<body><main><h1>Sign in to claude-remote</h1>{body}</main></body>
</html>"""
    return HTMLResponse(
        document,
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            # The request id is in the page's URL.
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                f"default-src 'none'; style-src 'unsafe-inline'; form-action {form_action}; "
                "frame-ancestors 'none'; base-uri 'none'"
            ),
        },
    )
