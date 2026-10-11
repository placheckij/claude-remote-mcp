# mcp-gateway

The MCP server that the Claude app talks to. It runs on the gateway host, is published
to the internet through a Cloudflare Tunnel, and is added to the Claude account as a
custom connector. Every tool call is checked against the connector's bearer token and
forwarded over the LAN to [`agent-bridge`](../agent-bridge/), which drives herdr on the agent host.

```
Claude app ──HTTPS──> Cloudflare edge ──tunnel──> cloudflared (gateway host) ──> mcp-gateway :8766 (gateway host)
                                                                           │ LAN, bearer token
                                                                           └─> agent-bridge :8765 (agent host)
```

Built on the MCP Python SDK 2.x (`MCPServer`, Streamable HTTP, stateless, JSON
responses). Tool results are short plain text meant to be read aloud: one line per
agent, the last few output lines, refusals from the agent host passed through verbatim.

## Tools

| Tool | Does |
|---|---|
| `list_agents` | one line per agent: id · name · status · folder |
| `agent_details` | the same line for one agent |
| `read_output` | last N meaningful terminal lines (default 20) |
| `send_prompt` | prompt, wait up to `wait_seconds` (default 90), return status + output; flags `blocked` and still-working |
| `wait_for_agent` | keep waiting for an agent still working (e.g. after `send_prompt` ran out of time); same result format |
| `answer_agent` | key presses for a blocked agent's question, e.g. `["1"]`, `["Enter"]`; with `wait_seconds` also waits for the result |
| `start_agent` | new agent in a project: a name under `MCP_GATEWAY_PROJECTS_ROOT` or an absolute path |
| `stop_agent` | close an agent's pane (marked destructive, so Claude asks first) |

The server's instructions tell Claude the user is probably driving: short, speakable
answers, no code read aloud, and no approvals without asking.

## Configuration

Read from the environment (on the gateway host: `/etc/mcp-gateway/env`, written by the setup script).

| Variable | |
|---|---|
| `MCP_GATEWAY_TOKEN` | required, 32+ chars; claude.ai sends it as `Authorization: Bearer …` |
| `MCP_GATEWAY_PUBLIC_HOST` | required; the tunnel hostname, e.g. `mcp.example.com` (requests with any other `Host` are refused) |
| `MCP_GATEWAY_BRIDGE_URL` | required, e.g. `http://192.168.1.20:8765` |
| `MCP_GATEWAY_BRIDGE_TOKEN` | required; agent-bridge's `AGENT_BRIDGE_TOKEN` |
| `MCP_GATEWAY_PROJECTS_ROOT` | default `/Users/me/projects`; where `start_agent` looks up project names (a path on the agent host) |

## Security

What stands between the internet and an agent on the agent host:

1. **Cloudflare Tunnel.** The gateway host opens an outbound connection; no port is opened on the
   router. Cloudflare terminates TLS.
2. **Ingress rule.** The tunnel only forwards `https://<host>/mcp`; every other path
   (including `/healthz`) gets a 404 at the edge.
3. **Bearer token.** mcp-gateway refuses any request without the exact
   `Authorization: Bearer <MCP_GATEWAY_TOKEN>` (constant-time compare).
4. **Host and Origin check.** Only `MCP_GATEWAY_PUBLIC_HOST` (and localhost) and the
   `https://claude.ai` origin are accepted (DNS-rebinding protection from the SDK).
5. **agent-bridge's own limits.** A separate token, a project-folder allowlist, the
   10-agent limit, and folder trust only under `~/projects`.
6. **Optional: Cloudflare Access** with a service token in front of the hostname (see
   below).

mcp-gateway listens on `127.0.0.1` only, so nothing on the LAN can reach it except
through the tunnel.

## Deploy with the setup script

[`deploy/setup.sh`](deploy/setup.sh) does the whole setup: it installs uv and
cloudflared, installs mcp-gateway as a systemd service, and creates and runs the
Cloudflare Tunnel.

### Before you start

- A Debian-based Linux with systemd (e.g. Debian 12+, Ubuntu 22.04+) with systemd. It
  coexists with other services: mcp-gateway uses port 8766 on localhost, and the tunnel runs
  as its own `cloudflared-claude-remote` service.
- A domain whose DNS is on Cloudflare (any plan, including Free). The MCP server gets a
  subdomain of it, e.g. `mcp.example.com`.
- agent-bridge running on the agent host (see [its README](../agent-bridge/README.md)), its LAN
  URL, and its token. Give the agent host a fixed LAN address (a DHCP reservation on the
  router) so the URL stays valid.

### Run it

```sh
git clone <this repo> ~/claude-remote-mcp
cd ~/claude-remote-mcp
sudo PUBLIC_HOST=mcp.example.com \
     AGENT_BRIDGE_URL=http://192.168.1.20:8765 \
     AGENT_BRIDGE_TOKEN='<AGENT_BRIDGE_TOKEN from the agent host>' \
     mcp-gateway/deploy/setup.sh
```

Values left out are asked for. Optional variables: `TUNNEL_NAME` (default
`claude-remote`), `PORT` (default `8766`), `PROJECTS_ROOT`, `ROTATE_TOKEN=1`.

On the first run `cloudflared tunnel login` prints a URL. Open it in any browser, log in
to Cloudflare and pick the zone (domain) for `PUBLIC_HOST`. The script waits until you
do.

At the end it checks that mcp-gateway is up, that the agent host answers with its token, and
that the tunnel answers `401` without the token and `404` outside `/mcp`. Then it prints
the connector settings and, on the first run, the new `MCP_GATEWAY_TOKEN`. DNS for a new
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

### Update, rotate, remove

- **Update the code:** `git pull` in the checkout, then re-run the script. It keeps the
  token and reuses the tunnel.
- **Rotate the connector token:** re-run with `ROTATE_TOKEN=1`, then update the request
  header in the claude.ai connector.
- **Change the agent host URL or token:** re-run with the new values.
- **Logs:** `journalctl -u mcp-gateway -u cloudflared-claude-remote -f`.
- **Remove:**
  ```sh
  sudo systemctl disable --now mcp-gateway cloudflared-claude-remote
  sudo rm /etc/systemd/system/{mcp-gateway,cloudflared-claude-remote}.service
  sudo TUNNEL_ORIGIN_CERT=/etc/cloudflared/cert.pem cloudflared tunnel delete claude-remote
  sudo rm -rf /opt/claude-remote-mcp /etc/mcp-gateway /etc/cloudflared/claude-remote.*
  ```
  Delete the DNS record for the hostname in the Cloudflare dashboard.

## Cloudflare Tunnel

The script uses a **locally managed** tunnel: its configuration lives in a file on the gateway
host, not in the Cloudflare dashboard. These are the steps it runs, if you'd rather do it
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

This depends on the same request-headers support as the bearer token (below). Without
it, leave Access off; the bearer token and the ingress rule still apply.

## Add the connector in claude.ai

claude.ai → **Customize → Connectors → Add custom connector**:

| Field | Value |
|---|---|
| URL | `https://mcp.example.com/mcp` |
| Authentication | **No sign-in** |
| Request headers | `authorization` = `Bearer <MCP_GATEWAY_TOKEN>` (plus the Access headers, if used) |

Connectors added on claude.ai are available in the Claude apps on the same account,
including on CarPlay. Request headers on custom connectors are a beta feature that not
every account has yet; without it the server would need OAuth, which isn't built.

## Development

```sh
uv sync --all-packages
uv run --package mcp-gateway pytest mcp-gateway

# Local run against a agent-bridge on this machine
MCP_GATEWAY_TOKEN=$(openssl rand -hex 32) MCP_GATEWAY_PUBLIC_HOST=localhost \
MCP_GATEWAY_BRIDGE_URL=http://127.0.0.1:8765 MCP_GATEWAY_BRIDGE_TOKEN=… \
  uv run --package mcp-gateway uvicorn mcp_gateway.server:main --factory --host 127.0.0.1 --port 8766
```

Tests drive the real MCP app over JSON-RPC with a fake agent-bridge behind
`httpx.MockTransport`. `deploy/setup.sh` is checked with `shellcheck`.
