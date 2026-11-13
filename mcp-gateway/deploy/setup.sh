#!/usr/bin/env bash
# Installs mcp-gateway as a systemd service on a Debian-based Linux host (e.g. Debian,
# Ubuntu) and publishes it through a Cloudflare Tunnel. Safe to re-run: it
# updates the code, keeps the sign-in passphrase, the OAuth tokens and the settings below,
# and reuses the existing tunnel.
#
# Run from a checkout of this repo on the gateway host:
#   sudo PUBLIC_HOST=mcp.example.com mcp-gateway/deploy/setup.sh
# On a re-run, values not given are taken from /etc/mcp-gateway/env; pass one to change
# it. Only PUBLIC_HOST is asked for, if missing. At the end the script opens a 30-minute
# enrollment window and prints the string for `agent-bridge enroll` on each agent host; open another
# later with `sudo mcp-gateway enroll`, which this script installs. The gateway listens on
# every interface for that; the tunnel publishes only /mcp and the OAuth paths.
# Don't put tokens on the command line (shell history, process list): let the script ask,
# or export them and pass them with sudo --preserve-env=NAME. Optional: AGENT_BRIDGE_URL
# (an agent host to add now; its token is asked for, or AGENT_BRIDGE_TOKEN), PROJECTS_ROOT (only for an agent-bridge without /info: the
# folder on the agent host that project names given to start_agent resolve against), TUNNEL_NAME
# (claude-remote), PORT (8766), MCP_GATEWAY_OAUTH_PASSPHRASE (use this sign-in passphrase
# instead of generating one), ROTATE_PASSPHRASE=1, MCP_GATEWAY_TOKEN (also accept this
# static token, from before OAuth), REMOVE_TOKEN=1 (stop accepting the static token).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_DIR=/opt/claude-remote-mcp
ENV_DIR=/etc/mcp-gateway
ENV_FILE="$ENV_DIR/env"
# systemd's StateDirectory=; holds the OAuth clients and token hashes.
STATE_DIR=/var/lib/mcp-gateway
SERVICE_USER=mcp-gateway
CF_DIR=/etc/cloudflared
CF_USER=cloudflared
TUNNEL_NAME="${TUNNEL_NAME:-claude-remote}"
PORT="${PORT:-8766}"
ORIGIN_CERT="$CF_DIR/cert.pem"
CREDENTIALS="$CF_DIR/$TUNNEL_NAME.json"
TUNNEL_CONFIG="$CF_DIR/$TUNNEL_NAME.yml"
TUNNEL_SERVICE="cloudflared-$TUNNEL_NAME"
# The MCP endpoint and the OAuth sign-in: its metadata, registration, the sign-in page,
# tokens and revocation. Keep in sync with mcp-gateway/README.md.
INGRESS_PATH='^/(mcp|authorize|login|token|register|revoke|\.well-known/oauth-(authorization-server|protected-resource/mcp))$'

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

ask() { # ask VAR "prompt" [secret]
  local var=$1 prompt=$2 secret=${3:-}
  if [[ -z "${!var:-}" ]]; then
    if [[ -n $secret ]]; then read -rsp "$prompt: " "${var?}"; echo; else read -rp "$prompt: " "${var?}"; fi
  fi
  [[ -n "${!var}" ]] || die "$var is required"
}

env_value() { # env_value KEY: KEY's value in $ENV_FILE, or nothing
  [[ -f $ENV_FILE ]] || return 0
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

[[ $EUID -eq 0 ]] || die "run with sudo"
if ! command -v apt-get >/dev/null || ! command -v systemctl >/dev/null; then
  die "this script needs a Debian-based Linux with systemd; see mcp-gateway/README.md to install by hand"
fi
[[ -f "$REPO_DIR/mcp-gateway/pyproject.toml" ]] || die "run from a checkout of claude-remote-mcp"

# A re-run keeps the last run's values for anything not given: updating the code needs no
# retyping, and a typo can't move the hostname.
kept=()
for name in PUBLIC_HOST PROJECTS_ROOT; do
  [[ -z ${!name:-} ]] || continue
  value="$(env_value "MCP_GATEWAY_$name")"
  [[ -n $value ]] || continue
  printf -v "$name" '%s' "$value"
  kept+=("$name=$value")
done
# The one agent-bridge from before bridges.json: kept in the env file until the gateway
# has imported it into the registry at its first start, then dropped (see the end).
LEGACY_URL=""
LEGACY_TOKEN=""
if [[ ! -e $STATE_DIR/bridges.json ]]; then
  LEGACY_URL="$(env_value MCP_GATEWAY_BRIDGE_URL)"
  LEGACY_TOKEN="$(env_value MCP_GATEWAY_BRIDGE_TOKEN)"
  # The legacy bridge is imported with a new AGENT_BRIDGE_TOKEN if given, with or without
  # its URL given again. Another URL is refused before anything changes: replacing the
  # legacy bridge here would lose it, and its token, if adding the new one then failed.
  if [[ -n $LEGACY_URL ]]; then
    [[ -z ${AGENT_BRIDGE_URL:-} || ${AGENT_BRIDGE_URL%/} == "${LEGACY_URL%/}" ]] \
      || die "$ENV_FILE still has the agent-bridge at $LEGACY_URL. Re-run without AGENT_BRIDGE_URL to import it, then change it with: sudo mcp-gateway bridges remove <name>, sudo mcp-gateway bridges add $AGENT_BRIDGE_URL"
    LEGACY_TOKEN="${AGENT_BRIDGE_TOKEN:-$LEGACY_TOKEN}"
    unset AGENT_BRIDGE_URL AGENT_BRIDGE_TOKEN
  fi
  [[ -z $LEGACY_URL ]] || kept+=("MCP_GATEWAY_BRIDGE_URL=$LEGACY_URL, to import as an agent host")
fi
if [[ ${#kept[@]} -gt 0 ]]; then
  log "Keeping from $ENV_FILE (pass a variable to change it):"
  printf '    %s\n' "${kept[@]}"
fi
# Kept from before agent-bridge had /info, it now does nothing there: say so, since names
# then resolve under the agent host's allowed roots, which may hold other folders.
if [[ -n ${PROJECTS_ROOT:-} ]]; then
  log "PROJECTS_ROOT is only used with an agent-bridge without /info. A newer one looks project names up in its own AGENT_BRIDGE_ALLOWED_ROOTS (the first root that has the folder)."
fi

ask PUBLIC_HOST "Public hostname for the MCP server (e.g. mcp.example.com)"
# Hostnames are case-insensitive, but the SDK's Host check compares them exactly.
PUBLIC_HOST="${PUBLIC_HOST,,}"

if [[ -n ${MCP_GATEWAY_OAUTH_PASSPHRASE:-} ]]; then
  [[ -z ${ROTATE_PASSPHRASE:-} ]] || die "set MCP_GATEWAY_OAUTH_PASSPHRASE or ROTATE_PASSPHRASE, not both"
  [[ ${#MCP_GATEWAY_OAUTH_PASSPHRASE} -ge 16 ]] \
    || die "MCP_GATEWAY_OAUTH_PASSPHRASE must be at least 16 characters"
  # No quotes, backslashes or $: systemd would interpret them in the env file.
  [[ $MCP_GATEWAY_OAUTH_PASSPHRASE =~ ^[A-Za-z0-9\ ._~+/=:,!?@#%^\&*()-]+$ ]] \
    || die "MCP_GATEWAY_OAUTH_PASSPHRASE may only contain letters, digits, spaces and . _ ~ + / = : , ! ? @ # % ^ & * ( ) -"
fi
if [[ -n ${MCP_GATEWAY_TOKEN:-} ]]; then
  [[ -z ${REMOVE_TOKEN:-} ]] || die "set MCP_GATEWAY_TOKEN or REMOVE_TOKEN, not both"
  [[ ${#MCP_GATEWAY_TOKEN} -ge 32 ]] || die "MCP_GATEWAY_TOKEN must be at least 32 characters"
  # The characters a bearer token may have (RFC 6750), which also keep the env file plain.
  [[ $MCP_GATEWAY_TOKEN =~ ^[A-Za-z0-9._~+/-]+=*$ ]] \
    || die "MCP_GATEWAY_TOKEN may only contain letters, digits, . _ ~ + / - and trailing ="
fi

# --- Packages ------------------------------------------------------------------------

log "Installing system packages"
apt-get update -qq
apt-get install -y -qq curl ca-certificates gnupg openssl rsync python3 git >/dev/null

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
# Only committed files (no .env, notes or caches from the checkout), owned by root: the
# service, and whoever owns the checkout, can run this code but not change it. The
# checkout belongs to another user, hence safe.directory for root's git.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
git -c safe.directory="$REPO_DIR" -c tar.umask=0022 -C "$REPO_DIR" archive --format=tar HEAD | tar -x -C "$STAGE"
chmod 0755 "$STAGE"
rsync -a --delete --chown=root:root --exclude /.venv --exclude /python "$STAGE/" "$APP_DIR/"
# Compile bytecode now: the sandboxed service can't write it into $APP_DIR, so without it
# every start compiles the dependencies again, which takes seconds on a small board.
(cd "$APP_DIR" && UV_PYTHON_INSTALL_DIR="$APP_DIR/python" uv sync --frozen --no-dev --package mcp-gateway --compile-bytecode)

log "Writing $ENV_FILE"
install -d -m 0750 -g "$SERVICE_USER" "$ENV_DIR"
# A passphrase passed in wins; otherwise keep the existing one, unless rotating.
PASSPHRASE="${MCP_GATEWAY_OAUTH_PASSPHRASE:-}"
PASSPHRASE_SOURCE=given
if [[ -z $PASSPHRASE && -z ${ROTATE_PASSPHRASE:-} ]]; then
  PASSPHRASE="$(env_value MCP_GATEWAY_OAUTH_PASSPHRASE)"
  PASSPHRASE_SOURCE=kept
fi
if [[ -z $PASSPHRASE ]]; then
  # 96 random bits in groups of four, e.g. 3f9a-0c41-…: typed once, on a phone.
  PASSPHRASE="$(openssl rand -hex 12 | sed 's/..../&-/g; s/-$//')"
  PASSPHRASE_SOURCE=new
fi
# The static token is only kept for connectors added before OAuth; none is made any more.
TOKEN="${MCP_GATEWAY_TOKEN:-}"
if [[ -z $TOKEN && -z ${REMOVE_TOKEN:-} ]]; then
  TOKEN="$(env_value MCP_GATEWAY_TOKEN)"
fi
umask 027
cat >"$ENV_FILE" <<EOF
MCP_GATEWAY_OAUTH_PASSPHRASE=$PASSPHRASE
MCP_GATEWAY_STATE_DIR=$STATE_DIR
${TOKEN:+MCP_GATEWAY_TOKEN=$TOKEN}
MCP_GATEWAY_PUBLIC_HOST=$PUBLIC_HOST
${LEGACY_URL:+MCP_GATEWAY_BRIDGE_URL=$LEGACY_URL}
${LEGACY_URL:+MCP_GATEWAY_BRIDGE_TOKEN=$LEGACY_TOKEN}
${PROJECTS_ROOT:+MCP_GATEWAY_PROJECTS_ROOT=$PROJECTS_ROOT}
EOF
umask 022
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

# `sudo mcp-gateway bridges …` runs as the service user, so bridges.json stays its own.
# AGENT_BRIDGE_TOKEN, if set, is passed on for `bridges add` without a terminal.
cat >/usr/local/bin/mcp-gateway <<EOF
#!/bin/sh
# Installed by mcp-gateway/deploy/setup.sh: mcp-gateway's commands, on the service's state.
exec sudo --preserve-env=AGENT_BRIDGE_TOKEN -u $SERVICE_USER $APP_DIR/.venv/bin/mcp-gateway --state-dir $STATE_DIR "\$@"
EOF
chmod 0755 /usr/local/bin/mcp-gateway

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
# One process (no --workers): it reads oauth.json only at startup and keeps sign-ins in progress
# and wrong-passphrase counts in memory.
# Every interface: agent-bridges on the LAN enroll here. The tunnel still connects to
# 127.0.0.1; other callers reach only the enrollment routes, which refuse what it forwards.
# --no-proxy-headers: uvicorn would otherwise take the tunnel's caller from X-Forwarded-For,
# and the gateway, seeing a caller other than 127.0.0.1, would answer 404 to everything.
ExecStart=$APP_DIR/.venv/bin/uvicorn mcp_gateway.server:main --factory --host 0.0.0.0 --port $PORT --no-server-header --no-proxy-headers
Restart=on-failure
RestartSec=5
# $STATE_DIR, the one place the service can write (ProtectSystem=strict).
StateDirectory=mcp-gateway
StateDirectoryMode=0700
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
  # Left from an earlier login; tunnel login wouldn't overwrite it anyway.
  if [[ -f /root/.cloudflared/cert.pem ]]; then
    log "Using the existing Cloudflare login cert /root/.cloudflared/cert.pem"
    echo "If it isn't for the zone that contains $PUBLIC_HOST, delete $ORIGIN_CERT and re-run."
  else
    log "Log in to Cloudflare: open the URL below and pick the zone that contains $PUBLIC_HOST"
    HOME=/root cloudflared tunnel login
  fi
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

# Only /mcp and the OAuth sign-in are published; the health check and anything else stay
# on this host.
cat >"$TUNNEL_CONFIG" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $CREDENTIALS
ingress:
  - hostname: $PUBLIC_HOST
    path: $INGRESS_PATH
    service: http://127.0.0.1:$PORT
  - service: http_status:404
EOF
cloudflared tunnel --config "$TUNNEL_CONFIG" ingress validate

# A dedicated unit, so it can sit next to any other cloudflared service on this host. The
# binary is the one found above, which isn't always the apt package's /usr/bin/cloudflared.
CLOUDFLARED="$(command -v cloudflared)"
cat >/etc/systemd/system/"$TUNNEL_SERVICE".service <<EOF
[Unit]
Description=Cloudflare Tunnel $TUNNEL_NAME for mcp-gateway
After=network-online.target mcp-gateway.service
Wants=network-online.target

[Service]
User=$CF_USER
Group=$CF_USER
ExecStart=$CLOUDFLARED --no-autoupdate tunnel --config $TUNNEL_CONFIG run
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

port_holder() { # the PID listening on TCP $PORT, or nothing
  # || true: lsof exits 1 when nothing listens, which pipefail would make fatal.
  if command -v ss >/dev/null; then
    ss -Hltnp "sport = :$PORT" | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -n 1 || true
  elif command -v lsof >/dev/null; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t | head -n 1 || true
  fi
}
# Taken only by the gateway being updated, which the restart below replaces.
HOLDER="$(port_holder)"
if [[ -n $HOLDER && $HOLDER != "$(systemctl show -p MainPID --value mcp-gateway.service 2>/dev/null)" ]]; then
  die "port $PORT is in use by $(ps -o comm= -p "$HOLDER" || echo "PID $HOLDER"); stop it, or re-run with PORT=<another port>"
fi

log "Starting services"
systemctl daemon-reload
systemctl enable --now mcp-gateway.service "$TUNNEL_SERVICE".service
systemctl restart mcp-gateway.service "$TUNNEL_SERVICE".service

FAILED=()
check() { # check [-w SECONDS] "label" expected-status curl-args...
  # -w retries for up to SECONDS while the status is wrong (curl args must not read stdin).
  local wait=0
  if [[ $1 == -w ]]; then wait=$2; shift 2; fi
  local label=$1 expected=$2; shift 2
  local status deadline=$((SECONDS + wait))
  while :; do
    status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true)"
    [[ $status != "$expected" && $SECONDS -lt $deadline ]] || break
    sleep 1
  done
  if [[ $status == "$expected" ]]; then
    echo "ok    $label ($status)"
  else
    echo "FAIL  $label (got $status, want $expected)"
    FAILED+=("$label")
  fi
}

log "Checks"
check -w 30 "mcp-gateway is up" 200 "http://127.0.0.1:$PORT/healthz"
# Without mcp-gateway the tunnel can only answer 502: then don't wait for it below.
tunnel_wait=30
[[ ${#FAILED[@]} -eq 0 ]] || tunnel_wait=0
# The gateway imports the legacy agent-bridge before it answers: once it is in
# bridges.json, the env file no longer needs it.
if [[ -n $LEGACY_URL && -e $STATE_DIR/bridges.json ]]; then
  sed -i '/^MCP_GATEWAY_BRIDGE_URL=/d; /^MCP_GATEWAY_BRIDGE_TOKEN=/d' "$ENV_FILE"
  echo "ok    imported $LEGACY_URL as an agent host; dropped it from $ENV_FILE"
elif [[ -n $LEGACY_URL ]]; then
  echo "FAIL  importing $LEGACY_URL as an agent host (see the logs; it is kept in $ENV_FILE)"
  FAILED+=("import of the agent host")
fi
if [[ ${#FAILED[@]} -eq 0 ]]; then
  if [[ -z ${AGENT_BRIDGE_URL:-} && -n ${AGENT_BRIDGE_TOKEN:-} ]]; then
    echo "FAIL  AGENT_BRIDGE_TOKEN is only used with AGENT_BRIDGE_URL, to add that host"
    FAILED+=("AGENT_BRIDGE_TOKEN without AGENT_BRIDGE_URL")
  elif [[ -n ${AGENT_BRIDGE_URL:-} ]] \
    && /usr/local/bin/mcp-gateway bridges list | awk '{print $2}' | grep -xF "${AGENT_BRIDGE_URL%/}" >/dev/null; then
    # A re-run with the same URL: nothing to add. A new token for it isn't taken silently.
    if [[ -n ${AGENT_BRIDGE_TOKEN:-} ]]; then
      echo "FAIL  $AGENT_BRIDGE_URL is already an agent host; to give it a new token:"
      echo "      sudo mcp-gateway bridges remove <name>, then sudo mcp-gateway bridges add $AGENT_BRIDGE_URL"
      FAILED+=("new token for $AGENT_BRIDGE_URL")
    else
      echo "ok    $AGENT_BRIDGE_URL is already an agent host"
    fi
  elif [[ -n ${AGENT_BRIDGE_URL:-} ]]; then
    /usr/local/bin/mcp-gateway bridges add "$AGENT_BRIDGE_URL" || FAILED+=("adding $AGENT_BRIDGE_URL")
  fi
  echo "Agent hosts:"
  /usr/local/bin/mcp-gateway bridges list | sed 's/^/  /' || true
fi
# The tunnel takes a few seconds to register at Cloudflare's edge; until then it answers 530.
# Once it has answered, it is registered, so the second check needn't wait.
check -w "$tunnel_wait" "tunnel refuses requests without a token" 401 -X POST "https://$PUBLIC_HOST/mcp"
check "tunnel serves the OAuth metadata" 200 "https://$PUBLIC_HOST/.well-known/oauth-protected-resource/mcp"
check "tunnel hides the rest" 404 "https://$PUBLIC_HOST/healthz"
check "tunnel hides enrollment" 404 -X POST "https://$PUBLIC_HOST/enroll"

if [[ ${#FAILED[@]} -eq 0 ]]; then
  cat <<EOF

Done. Add the connector in claude.ai -> Customize -> Connectors -> Add custom connector:
  URL:  https://$PUBLIC_HOST/mcp
  Leave the OAuth client ID and secret empty. Then click Connect and sign in with the
  passphrase below.
EOF
else
  printf '\nNot working yet: %d check(s) failed:\n' "${#FAILED[@]}"
  printf '  - %s\n' "${FAILED[@]}"
  cat <<EOF
Fix the cause (see the logs below) and re-run this script; it keeps the passphrase and tokens.
If only the tunnel checks failed for a new hostname, DNS may need a few more minutes:
  curl -i -X POST https://$PUBLIC_HOST/mcp    should answer 401.
EOF
fi
case $PASSPHRASE_SOURCE in
  new) echo "Sign-in passphrase (new): $PASSPHRASE" ;;
  given) echo "The sign-in passphrase is the one you passed in." ;;
  kept) echo "The sign-in passphrase is unchanged; read it with: sudo grep PASSPHRASE $ENV_FILE" ;;
esac
# A new passphrase only guards the next sign-in; tokens issued before stay valid.
if [[ $PASSPHRASE_SOURCE != kept && -s $STATE_DIR/oauth.json ]]; then
  # Stopped first: a running gateway writes the file back from memory on its next save.
  echo "Connectors already signed in stay signed in. To sign them out:"
  echo "  sudo systemctl stop mcp-gateway && sudo rm -f $STATE_DIR/oauth.json && sudo systemctl start mcp-gateway"
fi
if [[ -n $TOKEN ]]; then
  echo "The static MCP_GATEWAY_TOKEN is still accepted. Once the connector is signed in with"
  echo "OAuth, re-run with REMOVE_TOKEN=1 to stop accepting it."
fi
echo "Agent hosts: sudo mcp-gateway enroll (lets them add themselves), or bridges add <url> | list | rename <name> <new name> | remove <name>"
echo "Logs: journalctl -u mcp-gateway -u $TUNNEL_SERVICE -f"
[[ ${#FAILED[@]} -eq 0 ]] || exit 1
echo
/usr/local/bin/mcp-gateway enroll --port "$PORT" || echo "Couldn't open an enrollment window; run: sudo mcp-gateway enroll"
