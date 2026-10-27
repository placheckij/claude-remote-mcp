# mcp-gateway

The MCP server that the Claude app talks to. It runs on an always-on host (the
*gateway host*), is published to the internet through a Cloudflare Tunnel, and is added
to the Claude account as a custom connector. Every tool call is checked against the
connector's bearer token and forwarded over the LAN to
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
agent, the last few output lines, refusals from agent-bridge passed through verbatim.

## System requirements

| | |
|---|---|
| OS | anything that runs Python 3.11+; tested on Linux and macOS, Windows should work but is untested. The [setup script](#deploy-with-the-setup-script) needs a Debian-based Linux with systemd (e.g. Debian 12+, Ubuntu 22.04+); elsewhere, [install by hand](#install-by-hand). |
| Hardware | very light: one small Python process plus cloudflared. A single-board computer, a small home server, a VPS (if it can reach agent-bridge, e.g. over a VPN) or the agent host itself is enough. 64-bit recommended. |
| Software | [uv](https://docs.astral.sh/uv/) (installs Python 3.11+ itself if needed); [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) for the tunnel |
| Network | outbound HTTPS to Cloudflare; a route to agent-bridge's URL. No inbound port needs to be open. |
| Accounts | a Cloudflare account with a domain whose DNS is on Cloudflare (Free plan is fine); a Claude plan with custom connectors (Free, Pro and Max included) and the **Request headers** option in the Add custom connector dialog, a beta only some accounts have (see [Add the connector](#add-the-connector-in-claudeai)) |
| Always on | yes: the connector only works while this host is up |

## Tools

| Tool | Does |
|---|---|
| `list_agents` | one line per agent: id · name · status · folder |
| `agent_details` | the same line for one agent |
| `read_output` | last N meaningful terminal lines (default 20) |
| `send_prompt` | prompt, wait up to `wait_seconds` (default 90, at most 100), return status + output; flags `blocked` and still-working. A `blocked` agent refuses prompts until its question is answered |
| `wait_for_agent` | keep waiting for an agent still working (e.g. after `send_prompt` ran out of time); same result format |
| `answer_agent` | key presses for a blocked agent's question, e.g. `["1"]`, `["Enter"]`; with `wait_seconds` also waits for the result |
| `start_agent` | new agent in a project: a name under `MCP_GATEWAY_PROJECTS_ROOT` or an absolute path |
| `stop_agent` | close an agent's pane (marked destructive, so Claude asks first) |

The server's instructions tell Claude the user is probably driving: short, speakable
answers, no code read aloud, and no approvals without asking.

## Configuration

Read from the environment (with the setup script: `/etc/mcp-gateway/env`), or from a
`.env` file in the working directory (the environment wins). An empty variable counts as
unset. An invalid setting stops startup with one line per problem, naming the variable.

| Variable | |
|---|---|
| `MCP_GATEWAY_TOKEN` | required, 32+ chars; claude.ai sends it as `Authorization: Bearer …` |
| `MCP_GATEWAY_PUBLIC_HOST` | required; the tunnel hostname, e.g. `mcp.example.com` (requests with any other `Host` are refused) |
| `MCP_GATEWAY_BRIDGE_URL` | required, e.g. `http://192.168.1.20:8765` |
| `MCP_GATEWAY_BRIDGE_TOKEN` | required; agent-bridge's `AGENT_BRIDGE_TOKEN` |
| `MCP_GATEWAY_PROJECTS_ROOT` | required; where `start_agent` looks up project names, as a path on the agent host, e.g. `/home/me/projects` |

## Security

What stands between the internet and an agent:

1. **Cloudflare Tunnel.** The gateway host opens an outbound connection; no port is opened on the
   router. Cloudflare terminates TLS.
2. **Ingress rule.** The tunnel only forwards `https://<host>/mcp`; every other path
   (including `/healthz`) gets a 404 at the edge.
3. **Bearer token.** mcp-gateway refuses any request without
   `Authorization: Bearer <MCP_GATEWAY_TOKEN>`. The word `Bearer` may be in any case;
   the token must match exactly (constant-time compare).
4. **Host and Origin check.** Only `MCP_GATEWAY_PUBLIC_HOST` (and localhost) and the
   `https://claude.ai` origin are accepted (DNS-rebinding protection from the SDK).
5. **agent-bridge's own limits.** A separate token, a project-folder allowlist, the
   10-agent limit, and folder trust only under its trust root, never in the default
   Claude config.
6. **Optional: Cloudflare Access** with a service token in front of the hostname (see
   below).

mcp-gateway listens on `127.0.0.1` only, so nothing on the LAN can reach it except
through the tunnel.

## Deploy with the setup script

[`deploy/setup.sh`](deploy/setup.sh) does the whole setup: it installs uv and
cloudflared, installs mcp-gateway as a systemd service, and creates and runs the
Cloudflare Tunnel.

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
     AGENT_BRIDGE_TOKEN='<AGENT_BRIDGE_TOKEN from the agent host>' \
     PROJECTS_ROOT=/home/me/projects \
     mcp-gateway/deploy/setup.sh
```

Values left out are asked for. Optional variables: `TUNNEL_NAME` (default
`claude-remote`), `PORT` (default `8766`), `ROTATE_TOKEN=1`, and `MCP_GATEWAY_TOKEN`.

The script generates the connector token (`MCP_GATEWAY_TOKEN`) itself. To use one you
already have instead, e.g. when moving to a new gateway host, add it to the command:

```sh
sudo PUBLIC_HOST=mcp.example.com \
     AGENT_BRIDGE_URL=http://192.168.1.20:8765 \
     AGENT_BRIDGE_TOKEN='<AGENT_BRIDGE_TOKEN from the agent host>' \
     PROJECTS_ROOT=/home/me/projects \
     MCP_GATEWAY_TOKEN='<your connector token>' \
     mcp-gateway/deploy/setup.sh
```

It must be 32+ characters from `A-Z a-z 0-9 . _ ~ + / -`, optionally ending in `=`
(`openssl rand -hex 32` makes one).

On the first run `cloudflared tunnel login` prints a URL. Open it in any browser, log in
to Cloudflare and pick the zone (domain) for `PUBLIC_HOST`. The script waits until you
do.

At the end it checks that mcp-gateway is up, that agent-bridge answers with its token, and
that the tunnel answers `401` without the token and `404` outside `/mcp`. Then it prints
the connector settings and, on the first run (unless you passed one in), the new
`MCP_GATEWAY_TOKEN`. DNS for a new
hostname can take a minute; if only the tunnel checks fail, try again shortly:
`curl -i -X POST https://mcp.example.com/mcp` should answer `401`.

### What it creates

| Path | |
|---|---|
| `/opt/claude-remote-mcp/` | a copy of the repo and its `.venv` (root-owned; the service can't modify its code) |
| `/etc/mcp-gateway/env` | configuration and tokens (`root:mcp-gateway`, mode 640) |
| `/etc/systemd/system/mcp-gateway.service` | runs as the `mcp-gateway` system user on `127.0.0.1:8766`, sandboxed |
| `/etc/cloudflared/cert.pem` | Cloudflare account cert from `tunnel login`; only needed to manage tunnels, root-only |
| `/etc/cloudflared/claude-remote.json` | the tunnel's credentials (owned by the `cloudflared` user) |
| `/etc/cloudflared/claude-remote.yml` | tunnel config with the ingress rules |
| `/etc/systemd/system/cloudflared-claude-remote.service` | runs the tunnel as the `cloudflared` system user |

### Update and rotate

- **Update the code:** `git pull` in the checkout, then re-run the script. It keeps the
  token and reuses the tunnel.
- **Rotate the connector token:** re-run with `ROTATE_TOKEN=1`, then remove the
  connector in claude.ai and add it again with the new token. Request headers can't be
  edited once a connector is added.
- **Set the connector token yourself:** re-run with `MCP_GATEWAY_TOKEN=…`. Later runs
  keep it. If it differs from the one in claude.ai, re-add the connector there too.
- **Change agent-bridge's URL or token, or the projects folder:** re-run with the new values.
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

# 4. Remove the service users
sudo userdel mcp-gateway
sudo userdel cloudflared
```

Then:

- **DNS:** `tunnel delete` leaves the hostname's CNAME behind. Delete it in the
  Cloudflare dashboard (your domain → DNS → Records → the `mcp` record).
- **claude.ai:** remove the custom connector (Customize → Connectors).
- **Cloudflare Access**, if you set it up: delete the application and the service token
  in Zero Trust.
- **Only if nothing else on the host uses them:** the Cloudflare login cert, cloudflared
  itself and uv.
  ```sh
  sudo rm -f /etc/cloudflared/cert.pem
  sudo apt-get remove cloudflared
  sudo rm /etc/apt/sources.list.d/cloudflared.list /usr/share/keyrings/cloudflare-main.gpg
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
3. Publish only `/mcp` over HTTPS at `MCP_GATEWAY_PUBLIC_HOST`: a
   [Cloudflare Tunnel](#cloudflare-tunnel) set up by hand, or any reverse proxy that
   terminates TLS, with a request timeout of at least 125 s (`wait_seconds` goes up to
   100 s, and Cloudflare itself cuts requests off at 125 s).
   Don't publish `/healthz` or anything else.

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
    path: ^/mcp$            # publish only the MCP endpoint
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
**service token** and have the connector send it as headers:

1. Zero Trust → **Access → Service auth → Service tokens → Create**. Copy the Client
   ID and Client Secret.
2. Zero Trust → **Access → Applications → Add → Self-hosted**, domain
   `mcp.example.com`, with one policy: action **Service Auth**, include **Service
   Token** = the token from step 1.
3. In the claude.ai connector, add two more request headers:
   `CF-Access-Client-Id` and `CF-Access-Client-Secret`.

Two catches, both from claude.ai's request headers (below): custom header names like
these are only sent once Anthropic has approved them (ask
[Claude support](https://support.claude.com/en/articles/9015913-how-to-get-support);
until then the connector can't be saved with them), and a connector takes at most four
headers. Without approval, leave Access off; the bearer token and the ingress rule
still apply.

## Add the connector in claude.ai

claude.ai → **Customize → Connectors → Add custom connector**:

| Field | Value |
|---|---|
| URL | `https://mcp.example.com/mcp` |
| Authentication | **No sign-in** |
| Request headers | `authorization` = `Bearer <MCP_GATEWAY_TOKEN>` (plus the Access headers, if used) |

Enter the header value with the `Bearer ` prefix and the space: claude.ai sends it
exactly as typed.

**Request headers are a beta.** Only some accounts have the section in the dialog. If
yours doesn't, the connector can't send the token and mcp-gateway can't be connected:
it has no OAuth sign-in. Once a connector is added its headers can't be edited; to
change the token, remove the connector and add it again. See
[Claude's connector docs](https://claude.com/docs/connectors/custom/add-unlisted).

Connectors added on claude.ai are available in the Claude apps on the same account,
including the mobile app (and with it CarPlay).

## Development

```sh
uv sync --all-packages
uv run --package mcp-gateway pytest mcp-gateway

# Local run against an agent-bridge on this machine
MCP_GATEWAY_TOKEN=$(openssl rand -hex 32) MCP_GATEWAY_PUBLIC_HOST=localhost \
MCP_GATEWAY_BRIDGE_URL=http://127.0.0.1:8765 MCP_GATEWAY_BRIDGE_TOKEN=… \
MCP_GATEWAY_PROJECTS_ROOT=$HOME/projects \
  uv run --package mcp-gateway uvicorn mcp_gateway.server:main --factory --host 127.0.0.1 --port 8766
```

Tests drive the real MCP app over JSON-RPC with a fake agent-bridge behind
`httpx.MockTransport`. `deploy/setup.sh` is checked with `shellcheck`.
