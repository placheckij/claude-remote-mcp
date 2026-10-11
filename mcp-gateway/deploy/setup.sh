#!/usr/bin/env bash
# Installs mcp-gateway as a systemd service on a Debian-based Linux host and publishes it through a
# Cloudflare Tunnel. Safe to re-run: it updates the code, keeps the connector token and
# reuses the existing tunnel.
#
# Run from a checkout of this repo on the gateway host:
#   sudo PUBLIC_HOST=mcp.example.com \
#        AGENT_BRIDGE_URL=http://192.168.1.20:8765 \
#        AGENT_BRIDGE_TOKEN=<agent-bridge token> \
#        mcp-gateway/deploy/setup.sh
# Missing values are asked for interactively. Optional: TUNNEL_NAME (claude-remote),
# PORT (8766), PROJECTS_ROOT (/Users/me/projects), ROTATE_TOKEN=1.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_DIR=/opt/claude-remote-mcp
ENV_DIR=/etc/mcp-gateway
ENV_FILE="$ENV_DIR/env"
SERVICE_USER=mcp-gateway
CF_DIR=/etc/cloudflared
CF_USER=cloudflared
TUNNEL_NAME="${TUNNEL_NAME:-claude-remote}"
PORT="${PORT:-8766}"
PROJECTS_ROOT="${PROJECTS_ROOT:-/Users/me/projects}"
ORIGIN_CERT="$CF_DIR/cert.pem"
CREDENTIALS="$CF_DIR/$TUNNEL_NAME.json"
TUNNEL_CONFIG="$CF_DIR/$TUNNEL_NAME.yml"
TUNNEL_SERVICE="cloudflared-$TUNNEL_NAME"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

ask() { # ask VAR "prompt" [secret]
  local var=$1 prompt=$2 secret=${3:-}
  if [[ -z "${!var:-}" ]]; then
    if [[ -n $secret ]]; then read -rsp "$prompt: " "${var?}"; echo; else read -rp "$prompt: " "${var?}"; fi
  fi
  [[ -n "${!var}" ]] || die "$var is required"
}

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -f "$REPO_DIR/mcp-gateway/pyproject.toml" ]] || die "run from a checkout of claude-remote-mcp"

ask PUBLIC_HOST "Public hostname for the MCP server (e.g. mcp.example.com)"
ask AGENT_BRIDGE_URL "agent-bridge URL on the LAN (e.g. http://192.168.1.20:8765)"
ask AGENT_BRIDGE_TOKEN "agent-bridge token (AGENT_BRIDGE_TOKEN on the agent host)" secret

# --- Packages ------------------------------------------------------------------------

log "Installing system packages"
apt-get update -qq
apt-get install -y -qq curl ca-certificates gnupg openssl rsync python3 >/dev/null

if ! command -v uv >/dev/null; then
  log "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

if ! command -v cloudflared >/dev/null; then
  log "Installing cloudflared from Cloudflare's apt repository"
  install -d -m 0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
    >/etc/apt/sources.list.d/cloudflared.list
  apt-get update -qq
  apt-get install -y -qq cloudflared >/dev/null
fi

# --- mcp-gateway ----------------------------------------------------------------------

log "Installing mcp-gateway into $APP_DIR"
id "$SERVICE_USER" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -m 0755 "$APP_DIR"
rsync -a --delete --exclude /.git --exclude /.venv --exclude /python --exclude __pycache__ \
  "$REPO_DIR/" "$APP_DIR/"
# The code and venv stay root-owned: the service can run them but not change them.
(cd "$APP_DIR" && UV_PYTHON_INSTALL_DIR="$APP_DIR/python" uv sync --frozen --no-dev --package mcp-gateway)

log "Writing $ENV_FILE"
install -d -m 0750 -g "$SERVICE_USER" "$ENV_DIR"
TOKEN=""
if [[ -f $ENV_FILE && -z ${ROTATE_TOKEN:-} ]]; then
  TOKEN="$(sed -n 's/^MCP_GATEWAY_TOKEN=//p' "$ENV_FILE")"
fi
TOKEN_IS_NEW=""
if [[ -z $TOKEN ]]; then
  TOKEN="$(openssl rand -hex 32)"
  TOKEN_IS_NEW=1
fi
umask 027
cat >"$ENV_FILE" <<EOF
MCP_GATEWAY_TOKEN=$TOKEN
MCP_GATEWAY_PUBLIC_HOST=$PUBLIC_HOST
MCP_GATEWAY_BRIDGE_URL=$AGENT_BRIDGE_URL
MCP_GATEWAY_BRIDGE_TOKEN=$AGENT_BRIDGE_TOKEN
MCP_GATEWAY_PROJECTS_ROOT=$PROJECTS_ROOT
EOF
umask 022
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

cat >/etc/systemd/system/mcp-gateway.service <<EOF
[Unit]
Description=mcp-gateway (claude-remote MCP server)
After=network-online.target
Wants=network-online.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=$ENV_FILE
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/uvicorn mcp_gateway.server:main --factory --host 127.0.0.1 --port $PORT --no-server-header
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
LockPersonality=yes

[Install]
WantedBy=multi-user.target
EOF

# --- Cloudflare Tunnel ---------------------------------------------------------------

install -d -m 0755 "$CF_DIR"
id "$CF_USER" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$CF_USER"

if [[ ! -f $ORIGIN_CERT ]]; then
  log "Log in to Cloudflare: open the URL below and pick the zone for $PUBLIC_HOST"
  HOME=/root cloudflared tunnel login
  mv /root/.cloudflared/cert.pem "$ORIGIN_CERT"
  chmod 0600 "$ORIGIN_CERT"
fi
export TUNNEL_ORIGIN_CERT="$ORIGIN_CERT"

tunnel_id() {
  local tunnels
  tunnels="$(cloudflared tunnel list --name "$TUNNEL_NAME" --output json)" \
    || die "cannot list tunnels; if $ORIGIN_CERT is stale, delete it and re-run to log in again"
  python3 -c 'import json, sys; t = json.loads(sys.argv[1]) or []; print(t[0]["id"] if t else "")' "$tunnels"
}

TUNNEL_ID="$(tunnel_id)"
if [[ -z $TUNNEL_ID ]]; then
  log "Creating tunnel $TUNNEL_NAME"
  cloudflared tunnel create --credentials-file "$CREDENTIALS" "$TUNNEL_NAME"
  TUNNEL_ID="$(tunnel_id)"
elif [[ ! -f $CREDENTIALS ]]; then
  log "Fetching credentials for existing tunnel $TUNNEL_NAME"
  cloudflared tunnel token --credentials-file "$CREDENTIALS" "$TUNNEL_NAME"
fi
[[ -n $TUNNEL_ID ]] || die "could not find tunnel $TUNNEL_NAME"
chown "$CF_USER":"$CF_USER" "$CREDENTIALS"
chmod 0600 "$CREDENTIALS"

log "Routing $PUBLIC_HOST to tunnel $TUNNEL_NAME"
# Never overwrites an existing DNS record; if one exists for another target, fix it by hand.
cloudflared tunnel route dns "$TUNNEL_NAME" "$PUBLIC_HOST" \
  || echo "warning: DNS route not created (does a record for $PUBLIC_HOST already exist?)"

# Only /mcp is published; the health check and anything else stay on the gateway host.
cat >"$TUNNEL_CONFIG" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $CREDENTIALS
ingress:
  - hostname: $PUBLIC_HOST
    path: ^/mcp$
    service: http://127.0.0.1:$PORT
  - service: http_status:404
EOF
cloudflared tunnel --config "$TUNNEL_CONFIG" ingress validate

# A dedicated unit, so it can sit next to any other cloudflared service on the gateway host.
cat >/etc/systemd/system/"$TUNNEL_SERVICE".service <<EOF
[Unit]
Description=Cloudflare Tunnel $TUNNEL_NAME for mcp-gateway
After=network-online.target mcp-gateway.service
Wants=network-online.target

[Service]
User=$CF_USER
Group=$CF_USER
ExecStart=/usr/bin/cloudflared --no-autoupdate tunnel --config $TUNNEL_CONFIG run
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

# --- Start and check -----------------------------------------------------------------

log "Starting services"
systemctl daemon-reload
systemctl enable --now mcp-gateway.service "$TUNNEL_SERVICE".service
systemctl restart mcp-gateway.service "$TUNNEL_SERVICE".service

check() { # check "label" expected-status curl-args...
  local label=$1 expected=$2; shift 2
  local status
  status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true)"
  if [[ $status == "$expected" ]]; then echo "ok    $label ($status)"; else echo "FAIL  $label (got $status, want $expected)"; fi
}

log "Checks"
for _ in $(seq 20); do curl -fs "http://127.0.0.1:$PORT/healthz" >/dev/null && break; sleep 0.5; done
check "mcp-gateway is up" 200 "http://127.0.0.1:$PORT/healthz"
check "agent host reachable with its token" 200 -H "Authorization: Bearer $AGENT_BRIDGE_TOKEN" "$AGENT_BRIDGE_URL/agents"
sleep 3
check "tunnel refuses requests without the token" 401 -X POST "https://$PUBLIC_HOST/mcp"
check "tunnel hides everything but /mcp" 404 "https://$PUBLIC_HOST/healthz"

cat <<EOF

Done. Add the connector in claude.ai -> Customize -> Connectors -> Add custom connector:
  URL:             https://$PUBLIC_HOST/mcp
  Authentication:  No sign-in
  Request header:  authorization = Bearer <MCP_GATEWAY_TOKEN>
EOF
if [[ -n $TOKEN_IS_NEW ]]; then
  echo "MCP_GATEWAY_TOKEN (new): $TOKEN"
else
  echo "MCP_GATEWAY_TOKEN is unchanged; read it with: sudo grep MCP_GATEWAY_TOKEN $ENV_FILE"
fi
echo "Logs: journalctl -u mcp-gateway -u $TUNNEL_SERVICE -f"
