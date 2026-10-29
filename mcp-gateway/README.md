# mcp-gateway

The MCP server that the Claude app talks to. It runs on an always-on host (the
*gateway host*), is published to the internet through a Cloudflare Tunnel, and is added
to the Claude account as a custom connector, which signs in with OAuth. Every tool call
is checked against the connector's access token and forwarded over the LAN to
[`agent-bridge`](../agent-bridge/), which drives herdr on the *agent host*.

```
Claude app ──HTTPS──> Cloudflare edge ──tunnel──> cloudflared ──> mcp-gateway :8766     (gateway host)
                                                                      │ LAN, bearer token
                                                                      └─> agent-bridge :8765 (agent host)
```

The gateway host and the agent host can be the same machine; then agent-bridge listens
on `127.0.0.1` and nothing goes over the LAN.

Built on the MCP Python SDK 2.x (`MCPServer`, Streamable HTTP, stateless, JSON
responses). Tool results are short plain text meant to be read aloud: one line per
agent, the last few output lines. A refusal from agent-bridge comes back as a tool error
with agent-bridge's message unchanged, after the SDK's `Error executing tool <name>: `
prefix; invalid arguments (e.g. a malformed agent id, or a prompt over 20 000 characters
or with a NUL character) come back the same way as one sentence.

## System requirements

| | |
|---|---|
| OS | anything that runs Python 3.11+; tested on Linux and macOS, Windows should work but is untested. The [setup script](#deploy-with-the-setup-script) needs a Debian-based Linux with systemd (e.g. Debian 12+, Ubuntu 22.04+); elsewhere, [install by hand](#install-by-hand). |
| Hardware | very light: one small Python process plus cloudflared. A single-board computer, a small home server, a VPS (if it can reach agent-bridge, e.g. over a VPN) or the agent host itself is enough. 64-bit recommended. |
| Software | [uv](https://docs.astral.sh/uv/) (installs Python 3.11+ itself if needed); [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) for the tunnel |
| Network | outbound HTTPS to Cloudflare; a route to agent-bridge's URL. No inbound port needs to be open. |
| Accounts | a Cloudflare account with a domain whose DNS is on Cloudflare (Free plan is fine); a Claude plan with custom connectors (Free, Pro and Max included) |
| Always on | yes: the connector only works while this host is up |

## Tools

| Tool | Does |
|---|---|
| `list_agents` | one line per agent: id · name · status · folder (a folder under `MCP_GATEWAY_PROJECTS_ROOT` by its name there, e.g. `grafik`) |
| `agent_details` | the same line for one agent |
| `read_output` | last N meaningful terminal lines (default 20, at most 60; the same for `lines` on the waiting tools) |
| `send_prompt` | prompt, wait up to `wait_seconds` (default 90, at most 100), return status + output; flags `blocked` and still-working. A `blocked` agent refuses prompts until its question is answered |
| `wait_for_agent` | keep waiting for an agent still working (e.g. after `send_prompt` ran out of time); same result format |
| `answer_agent` | key presses for a blocked agent's question, e.g. `["1"]`, `["Enter"]`; with `wait_seconds` also waits for the result. Marked destructive, so Claude asks first |
| `start_agent` | new agent in a project: one folder name under `MCP_GATEWAY_PROJECTS_ROOT` (names with `/`, `..` or `~`, and `.`, are refused) or an absolute path |
| `stop_agent` | close an agent's pane (marked destructive, so Claude asks first) |

The server's instructions tell Claude the user is probably driving: short, speakable
answers, no code read aloud, and no approvals without asking.

## Configuration

Read from the environment (with the setup script: `/etc/mcp-gateway/env`), or from a
`.env` file in the working directory (the environment wins). An empty variable counts as
unset. An invalid setting stops startup with one line per problem, naming the variable.

| Variable | |
|---|---|
| `MCP_GATEWAY_OAUTH_PASSPHRASE` | required, 16+ chars; typed on the [sign-in page](#oauth-sign-in) when the connector is added |
| `MCP_GATEWAY_STATE_DIR` | required; where the signed-in clients and token hashes are kept (`oauth.json`, mode 600), e.g. `/var/lib/mcp-gateway` |
| `MCP_GATEWAY_TOKEN` | optional, 32+ chars; the static token from before OAuth, still accepted as `Authorization: Bearer …` while set. Leave it unset unless a connector added with request headers still uses it |
| `MCP_GATEWAY_PUBLIC_HOST` | required; the tunnel hostname, e.g. `mcp.example.com` (requests with any other `Host` are refused). The OAuth issuer is `https://<host>` and the connector URL `https://<host>/mcp` |
| `MCP_GATEWAY_BRIDGE_URL` | required, e.g. `http://192.168.1.20:8765` |
| `MCP_GATEWAY_BRIDGE_TOKEN` | required; agent-bridge's `AGENT_BRIDGE_TOKEN` |
| `MCP_GATEWAY_PROJECTS_ROOT` | required; where `start_agent` looks up project names, as a path on the agent host, e.g. `/home/me/projects` |

## Security

What stands between the internet and an agent:

1. **Cloudflare Tunnel.** The gateway host opens an outbound connection; no port is opened on the
   router. Cloudflare terminates TLS.
2. **Ingress rule.** The tunnel only forwards `/mcp` and the OAuth sign-in's paths
   (see [OAuth sign-in](#oauth-sign-in)); every other path (including `/healthz`) gets a
   404 from cloudflared on the gateway host and never reaches mcp-gateway.
3. **OAuth access token.** mcp-gateway refuses any `/mcp` request without
   `Authorization: Bearer <token>`, where the token is an unexpired access token it
   issued (or `MCP_GATEWAY_TOKEN`, while set; constant-time compare). The word `Bearer`
   may be in any case. The 401 carries
   `WWW-Authenticate: Bearer resource_metadata="https://<host>/.well-known/oauth-protected-resource/mcp"`,
   from which claude.ai starts the sign-in, and each refusal is logged with the request's
   headers, secrets redacted.
4. **Host and Origin check.** Only `MCP_GATEWAY_PUBLIC_HOST` (and localhost) is
   accepted as `Host`, and only `https://claude.ai` or `https://claude.com` as `Origin`
   (a request without one is fine). This is DNS-rebinding protection from the SDK. A
   refused Origin with a valid token is logged as an error: if it is the Claude app,
   every call fails until that Origin is added to `ALLOWED_ORIGINS` in
   `src/mcp_gateway/server.py`.

`GET /mcp` answers `405`: the server is stateless and never pushes anything on a GET
stream, so clients use POST only, as the MCP spec allows.
5. **agent-bridge's own limits.** A separate token, a project-folder allowlist, the
   10-agent limit, and folder trust only under its trust root, never in the default
   Claude config.
6. **Optional: Cloudflare Access** with a service token in front of the hostname (see
   below).

mcp-gateway listens on `127.0.0.1` only, so nothing on the LAN can reach it except
through the tunnel.

## OAuth sign-in

mcp-gateway is its own OAuth authorization server, for one user with one passphrase. When
the connector is added, claude.ai:

1. gets the `401` from `/mcp` and reads `/.well-known/oauth-protected-resource/mcp`, then
   `/.well-known/oauth-authorization-server`;
2. registers itself at `/register` (dynamic client registration, as a public client);
3. opens `/authorize`, which forwards to the sign-in page `/login`. It names the client
   and the site the browser returns to (claude.ai), and asks for
   `MCP_GATEWAY_OAUTH_PASSPHRASE`;
4. trades the code for tokens at `/token` (PKCE, S256) and calls `/mcp` with them.

| | |
|---|---|
| Access token | 1 hour; claude.ai refreshes it by itself |
| Refresh token | 90 days; replaced on every refresh (the old one stops working) |
| Callback | only `https://claude.ai/api/mcp/auth_callback` can be registered, so a sign-in can't hand a code to anyone else. Claude Code's loopback callback isn't accepted |
| Wrong passphrases | logged; after 10 within 15 minutes, sign-in is refused until they age out. Anyone can open a sign-in page, so a stranger can also hold sign-in off this way; tokens already issued keep working |
| Storage | `MCP_GATEWAY_STATE_DIR/oauth.json`: the clients that signed in and SHA-256 hashes of their tokens, never the tokens. A registration that never signs in is kept in memory only (at most 100) |
| Revocation | `/revoke` ends a token and its pair |
| Restart | tokens survive it; a sign-in page or code that was open is lost (sign in again) |

To sign every connector out, stop the service, delete `oauth.json` and start it again.
Changing the passphrase only affects the next sign-in.

## Deploy with the setup script

[`deploy/setup.sh`](deploy/setup.sh) does the whole setup: it installs uv and
cloudflared, installs mcp-gateway as a systemd service, creates and runs the
Cloudflare Tunnel, and generates the sign-in passphrase.

### Before you start

- A Debian-based Linux with systemd (see [System requirements](#system-requirements)).
  It coexists with other services: mcp-gateway uses port 8766 on localhost, and the
  tunnel runs as its own `cloudflared-claude-remote` service next to any other
  cloudflared setup.
- A domain whose DNS is on Cloudflare (any plan, including Free). The MCP server gets a
  subdomain of it, e.g. `mcp.example.com`.
- agent-bridge running on the agent host (see [its README](../agent-bridge/README.md)),
  its LAN URL, and its token. Give the agent host a fixed LAN address (a DHCP
  reservation on the router) so the URL stays valid.
- The projects folder on the agent host that `start_agent` should resolve project names
  against.

### Run it

```sh
git clone <this repo> ~/claude-remote-mcp
cd ~/claude-remote-mcp
sudo PUBLIC_HOST=mcp.example.com \
     AGENT_BRIDGE_URL=http://192.168.1.20:8765 \
     PROJECTS_ROOT=/home/me/projects \
     mcp-gateway/deploy/setup.sh
```

It asks for agent-bridge's token (`AGENT_BRIDGE_TOKEN` from the agent host) without
showing it. Keep tokens out of the command itself: there they end up in your shell
history and, while the script runs, in the process list that other users on the host
can read. Other values left out are asked for too; on later runs they come from
`/etc/mcp-gateway/env` instead (see [Update and rotate](#update-and-rotate)). Optional
variables: `TUNNEL_NAME` (default `claude-remote`), `PORT` (default `8766`),
`MCP_GATEWAY_OAUTH_PASSPHRASE`, `ROTATE_PASSPHRASE=1`, `MCP_GATEWAY_TOKEN` and
`REMOVE_TOKEN=1`.

The script generates the sign-in passphrase (`MCP_GATEWAY_OAUTH_PASSPHRASE`, 96 random
bits as six groups of four hex digits) itself. To choose your own instead, read it
without echoing and pass it through sudo's environment:

```sh
read -rsp 'Passphrase: ' MCP_GATEWAY_OAUTH_PASSPHRASE && export MCP_GATEWAY_OAUTH_PASSPHRASE && echo
sudo --preserve-env=MCP_GATEWAY_OAUTH_PASSPHRASE \
     PUBLIC_HOST=mcp.example.com \
     AGENT_BRIDGE_URL=http://192.168.1.20:8765 \
     PROJECTS_ROOT=/home/me/projects \
     mcp-gateway/deploy/setup.sh
```

It must be 16+ characters from letters, digits, spaces and `. _ ~ + / = : , ! ? @ # % ^ & * ( ) -`.

No static token is made any more. One already in `/etc/mcp-gateway/env` is kept, so a
connector added with request headers keeps working until it is replaced (see
[Move a connector to OAuth](#move-a-connector-to-oauth)); `MCP_GATEWAY_TOKEN` passed the
same way sets one.

On the first run `cloudflared tunnel login` prints a URL. Open it in any browser, log in
to Cloudflare and pick the zone (domain) that contains `PUBLIC_HOST`: `example.com` for
`mcp.example.com`. The script waits until you do. The DNS record is created in the zone
picked here; in any other zone the name comes out wrong (e.g.
`mcp.example.com.other.org`). If that happened, delete that record in the dashboard,
delete `/etc/cloudflared/cert.pem` and re-run to log in again. A cert left in
`/root/.cloudflared/` by an earlier `cloudflared tunnel login` is used instead of logging
in.

At the end it checks that mcp-gateway is up, that agent-bridge answers with its token, and
that the tunnel answers `401` on `/mcp` without a token, serves the OAuth metadata, and
answers `404` elsewhere (waiting up to 30 s each for the service and the tunnel to come
up). If all pass, it prints the connector settings; if any fails, it lists the failed
checks and exits with status 1. Either way it says where the passphrase is: when it
makes a new one, it prints it. DNS for a new hostname can take a few minutes; if only
the tunnel checks fail, try again shortly:
`curl -i -X POST https://mcp.example.com/mcp` should answer `401`.

### What it creates

| Path | |
|---|---|
| `/opt/claude-remote-mcp/` | the checkout's committed files (`HEAD`; no untracked or uncommitted changes) and their `.venv`, all owned by root, so neither the service nor the checkout's owner can change the code it runs |
| `/etc/mcp-gateway/env` | configuration, passphrase and tokens (`root:mcp-gateway`, mode 640) |
| `/var/lib/mcp-gateway/` | `oauth.json`: signed-in clients and token hashes (systemd `StateDirectory`, owned by `mcp-gateway`, mode 700) |
| `/etc/systemd/system/mcp-gateway.service` | runs as the `mcp-gateway` system user on `127.0.0.1:8766`, sandboxed |
| `/etc/cloudflared/cert.pem` | Cloudflare account cert from `tunnel login`; only needed to manage tunnels, root-only |
| `/etc/cloudflared/claude-remote.json` | the tunnel's credentials (owned by the `cloudflared` user) |
| `/etc/cloudflared/claude-remote.yml` | tunnel config with the ingress rules |
| `/etc/systemd/system/cloudflared-claude-remote.service` | runs the tunnel as the `cloudflared` system user |

### Update and rotate

- **Update the code:** `git pull` in the checkout, then re-run the script without any
  variables: `sudo mcp-gateway/deploy/setup.sh`. It takes `PUBLIC_HOST`,
  `AGENT_BRIDGE_URL`, `AGENT_BRIDGE_TOKEN` and `PROJECTS_ROOT` from
  `/etc/mcp-gateway/env` and lists the ones it keeps (tokens not shown), keeps the
  passphrase, the OAuth tokens and the static token if there is one, and reuses the
  tunnel. Only committed code is installed.
- **Change the passphrase:** re-run with `ROTATE_PASSPHRASE=1` (it prints the new one),
  or with `MCP_GATEWAY_OAUTH_PASSPHRASE` as above. Connectors already signed in stay
  signed in; to sign them out too, `sudo rm /var/lib/mcp-gateway/oauth.json && sudo
  systemctl restart mcp-gateway`.
- **Stop accepting the static token:** re-run with `REMOVE_TOKEN=1`.
- **Change agent-bridge's URL or token, or the projects folder:** re-run with just the
  values that change; the rest are kept:
  ```sh
  sudo AGENT_BRIDGE_URL=http://192.168.1.21:8765 mcp-gateway/deploy/setup.sh
  ```
  For a new agent-bridge token, export it as shown for `MCP_GATEWAY_TOKEN` above and
  pass it with `sudo --preserve-env=AGENT_BRIDGE_TOKEN`.
- **Change the hostname:** re-run with the new `PUBLIC_HOST`. It gets a new DNS record;
  delete the old one in the Cloudflare dashboard, and re-add the connector in claude.ai
  with the new URL.
- **Logs:** `journalctl -u mcp-gateway -u cloudflared-claude-remote -f`.

### Stop, restart, uninstall

The MCP server and the tunnel are two services. Stopping the tunnel alone is enough to
take the connector offline; claude.ai then gets an error from Cloudflare.

```sh
# Status
systemctl status mcp-gateway cloudflared-claude-remote

# Take it offline: stop the tunnel (and the server); they start again at boot
sudo systemctl stop cloudflared-claude-remote mcp-gateway

# Keep it offline across reboots too
sudo systemctl disable --now cloudflared-claude-remote mcp-gateway

# Back online
sudo systemctl enable --now mcp-gateway cloudflared-claude-remote

# Restart, e.g. after editing /etc/mcp-gateway/env by hand
sudo systemctl restart mcp-gateway cloudflared-claude-remote
```

**Uninstall everything** the script set up:

```sh
# 1. Stop and remove both services
sudo systemctl disable --now cloudflared-claude-remote mcp-gateway
sudo rm /etc/systemd/system/mcp-gateway.service /etc/systemd/system/cloudflared-claude-remote.service
sudo systemctl daemon-reload

# 2. Delete the tunnel at Cloudflare (it must have no running connections, hence step 1)
sudo TUNNEL_ORIGIN_CERT=/etc/cloudflared/cert.pem cloudflared tunnel cleanup claude-remote
sudo TUNNEL_ORIGIN_CERT=/etc/cloudflared/cert.pem cloudflared tunnel delete claude-remote

# 3. Remove the code, configuration, tokens and tunnel credentials
sudo rm -rf /opt/claude-remote-mcp /etc/mcp-gateway
sudo rm -f /etc/cloudflared/claude-remote.json /etc/cloudflared/claude-remote.yml

# 4. Remove the service user
sudo userdel mcp-gateway
```

Then:

- **DNS:** `tunnel delete` leaves the hostname's CNAME behind. Delete it in the
  Cloudflare dashboard (your domain → DNS → Records → the `mcp` record).
- **claude.ai:** remove the custom connector (Customize → Connectors).
- **Cloudflare Access**, if you set it up: delete the application and the service token
  in Zero Trust.
- **Only if nothing else on the host uses them:** the Cloudflare login cert, the
  `cloudflared` user, cloudflared itself, and uv with root's uv cache. The script reuses
  a `cloudflared` user that already exists, so check first that no other service runs as
  it: `ps -u cloudflared` and `grep -rl '^User=cloudflared' /etc/systemd/system` should
  find nothing.
  ```sh
  sudo rm -f /etc/cloudflared/cert.pem
  sudo rmdir /etc/cloudflared /root/.cloudflared   # only removed if empty
  sudo userdel cloudflared
  sudo apt-get remove cloudflared
  sudo rm /etc/apt/sources.list.d/cloudflared.list /usr/share/keyrings/cloudflare-main.gpg
  sudo rm -rf /root/.cache/uv
  sudo rm /usr/local/bin/uv /usr/local/bin/uvx
  ```
  Treat the cert like a password while it exists: it can create tunnels and DNS
  records in that zone.

## Install by hand

On any other system (another Linux, macOS, Windows), or to use a different tunnel or
reverse proxy:

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/), clone the
   repo, and run `uv sync --frozen --no-dev --package mcp-gateway`.
2. Set the [configuration](#configuration) variables and start the server on
   localhost:
   ```sh
   uv run --frozen --no-dev --package mcp-gateway \
     uvicorn mcp_gateway.server:main --factory --host 127.0.0.1 --port 8766 --no-server-header
   ```
   Keep it running with your system's service manager (systemd, launchd, NSSM, …).
3. Publish over HTTPS at `MCP_GATEWAY_PUBLIC_HOST` only `/mcp` and the OAuth paths:
   `/authorize`, `/login`, `/token`, `/register`, `/revoke`,
   `/.well-known/oauth-authorization-server` and
   `/.well-known/oauth-protected-resource/mcp`. Use a
   [Cloudflare Tunnel](#cloudflare-tunnel) set up by hand, or any reverse proxy that
   terminates TLS, with a request timeout of at least 125 s (`wait_seconds` goes up to
   100 s, and Cloudflare itself cuts requests off at 125 s).
   Don't publish `/healthz` or anything else. mcp-gateway needs a writable
   `MCP_GATEWAY_STATE_DIR`.

## Cloudflare Tunnel

The script uses a **locally managed** tunnel: its configuration lives in a file on the
gateway host, not in the Cloudflare dashboard. These are the steps it runs, if you'd rather do it
by hand:

```sh
# 1. Authorize cloudflared for your Cloudflare account and zone (writes ~/.cloudflared/cert.pem)
cloudflared tunnel login

# 2. Create the tunnel and its credentials file
cloudflared tunnel create --credentials-file /etc/cloudflared/claude-remote.json claude-remote

# 3. Point the hostname at the tunnel (creates a proxied CNAME to <tunnel-id>.cfargotunnel.com)
cloudflared tunnel route dns claude-remote mcp.example.com
```

`/etc/cloudflared/claude-remote.yml`:

```yaml
tunnel: <tunnel id from `cloudflared tunnel list`>
credentials-file: /etc/cloudflared/claude-remote.json
ingress:
  - hostname: mcp.example.com
    # only the MCP endpoint and the OAuth sign-in
    path: ^/(mcp|authorize|login|token|register|revoke|\.well-known/oauth-(authorization-server|protected-resource/mcp))$
    service: http://127.0.0.1:8766
  - service: http_status:404
```

```sh
# 4. Check the rules, then run it (the script wraps this in a systemd unit)
cloudflared tunnel --config /etc/cloudflared/claude-remote.yml ingress validate
cloudflared tunnel --config /etc/cloudflared/claude-remote.yml ingress rule https://mcp.example.com/mcp
cloudflared --no-autoupdate tunnel --config /etc/cloudflared/claude-remote.yml run
```

Updates to cloudflared arrive through apt (`sudo apt upgrade`), which is why the
service runs with `--no-autoupdate`. In the dashboard the tunnel shows up under
**Zero Trust → Networks → Tunnels**, where its connection status is visible.

### Optional: Cloudflare Access with a service token

Access can sit in front of the hostname as a second gate, checked by Cloudflare before
a request ever reaches the gateway host. claude.ai can't complete an Access login page, so use a
**service token** and have the connector send it as headers.

This doesn't fit the OAuth sign-in well: `/authorize` and `/login` open in your browser,
which has no service token, so those two paths need an Access **Bypass** policy, and the
connector still needs request headers (below) for the rest. With OAuth on, leave Access
off unless you need it.

1. Zero Trust → **Access → Service auth → Service tokens → Create**. Copy the Client
   ID and Client Secret.
2. Zero Trust → **Access → Applications → Add → Self-hosted**, domain
   `mcp.example.com`, with one policy: action **Service Auth**, include **Service
   Token** = the token from step 1.
3. In the claude.ai connector, add two more request headers:
   `CF-Access-Client-Id` and `CF-Access-Client-Secret`.

Two catches, both from claude.ai's request headers, a beta only some accounts have:
custom header names like these are only sent once Anthropic has approved them (ask
[Claude support](https://support.claude.com/en/articles/9015913-how-to-get-support);
until then the connector can't be saved with them), and a connector takes at most four
headers. Without approval, leave Access off; the OAuth token and the ingress rule still
apply.

## Add the connector in claude.ai

claude.ai → **Customize → Connectors → Add custom connector**:

| Field | Value |
|---|---|
| URL | `https://mcp.example.com/mcp` |
| OAuth client ID and secret (advanced settings) | leave empty: claude.ai registers itself |

Then click **Connect**. A page from your gateway asks for the passphrase
(`MCP_GATEWAY_OAUTH_PASSPHRASE`); after **Allow** you are back in claude.ai, connected.
claude.ai keeps the connector signed in by refreshing its token; if it was signed out
(e.g. after 90 days unused, or `oauth.json` was deleted), it asks you to connect again.
See [Claude's connector docs](https://claude.com/docs/connectors/custom/add-unlisted).

### Move a connector to OAuth

A connector added before OAuth sends the static token as a request header, which can't
be edited. To move it over:

1. Update the gateway (re-run the setup script; it keeps the static token, so the old
   connector keeps working meanwhile, and prints the passphrase).
2. In claude.ai, remove the old connector and add it again as above, signing in with the
   passphrase.
3. Re-run the setup script with `REMOVE_TOKEN=1`, so the static token stops working.

Connectors added on claude.ai are available in the Claude apps on the same account,
including the mobile app (and with it CarPlay).

## Development

```sh
uv sync --all-packages
uv run --package mcp-gateway pytest mcp-gateway

# Local run against an agent-bridge on this machine
MCP_GATEWAY_OAUTH_PASSPHRASE=$(openssl rand -hex 12) MCP_GATEWAY_STATE_DIR=/tmp/mcp-gateway-state \
MCP_GATEWAY_TOKEN=$(openssl rand -hex 32) MCP_GATEWAY_PUBLIC_HOST=localhost \
MCP_GATEWAY_BRIDGE_URL=http://127.0.0.1:8765 MCP_GATEWAY_BRIDGE_TOKEN=… \
MCP_GATEWAY_PROJECTS_ROOT=$HOME/projects \
  uv run --package mcp-gateway uvicorn mcp_gateway.server:main --factory --host 127.0.0.1 --port 8766
```

Tests drive the real MCP app over JSON-RPC with a fake agent-bridge behind
`httpx.MockTransport`; `test_oauth.py` goes through the whole sign-in the way claude.ai
does. `deploy/setup.sh` is checked with `shellcheck`.
