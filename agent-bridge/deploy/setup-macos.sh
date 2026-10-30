#!/usr/bin/env bash
# Installs agent-bridge as a launchd agent on macOS: it runs from this checkout, starts
# at login and restarts when it stops. Safe to re-run, e.g. after pulling new code: it
# keeps the token and offers the installed settings as defaults.
#
# Run as your own user (not sudo), from a checkout of this repo on the agent host:
#   agent-bridge/deploy/setup-macos.sh
# Missing values are asked for interactively; pass them in to skip the questions:
#   HOST=192.168.1.20 ALLOWED_ROOTS=/Users/me/projects agent-bridge/deploy/setup-macos.sh
# HOST is the LAN address to listen on, ALLOWED_ROOTS the `:`-separated folders agents may
# start in. Optional: PORT (8765), AGENT_BRIDGE_CLAUDE_CONFIG_DIR and TRUST_ROOT (folder
# trust, see agent-bridge/README.md; not CLAUDE_CONFIG_DIR, which Claude Code itself reads
# and may already be set), HERDR_BIN (found on PATH), ROTATE_TOKEN=1, AGENT_BRIDGE_TOKEN
# (use this token instead of generating one), AGENT_BRIDGE_ACCOUNTS (named Claude logins as
# JSON, see agent-bridge/README.md; kept from the installed settings, empty removes them).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LABEL=dev.claude-remote.agent-bridge
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_FILE="$HOME/Library/Logs/agent-bridge.log"
DOMAIN="gui/$(id -u)"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

installed() { # installed KEY: a value from the installed plist, or nothing
  [[ -f $PLIST ]] && /usr/libexec/PlistBuddy -c "Print :$1" "$PLIST" 2>/dev/null || true
}

ask() { # ask VAR "prompt" [default]: keeps a value passed in; Enter takes the default
  local var=$1 prompt=$2 default=${3:-} answer
  if [[ -z ${!var+x} ]]; then
    # No input left (stdin not a terminal) takes the default too.
    read -rp "$prompt${default:+ [$default]}: " answer || true
    printf -v "$var" '%s' "${answer:-$default}"
  fi
  [[ -n ${!var} ]] || die "$var is required"
}

ask_optional() { # like ask, but may stay empty: Enter skips, "-" clears a default
  local var=$1 prompt=$2 default=${3:-} answer hint="Enter to skip"
  [[ -z $default ]] || hint="$default; Enter for that, - for none"
  if [[ -z ${!var+x} ]]; then
    read -rp "$prompt [$hint]: " answer || true
    printf -v "$var" '%s' "${answer:-$default}"
  fi
  # Through '%s': bash 3.2's printf -v leaves the variable as it was on an empty format.
  [[ ${!var} != - ]] || printf -v "$var" '%s' ''
}

absolute() { # absolute VAR [name]: expands a leading ~ and refuses relative paths
  local var=$1 path=${!1} name=${2:-$1}
  # shellcheck disable=SC2088  # matching a literal ~ typed at the prompt
  case $path in
    "~") path=$HOME ;;
    "~/"*) path="$HOME/${path#"~/"}" ;;
  esac
  [[ -z $path || $path == /* ]] || die "$name must be a full path, got $path"
  printf -v "$var" '%s' "$path"
}

lan_address() { # the address of the interface the default route goes through
  local iface
  iface="$(route -n get default 2>/dev/null | awk '/interface:/ {print $2}')"
  [[ -n $iface ]] && ipconfig getifaddr "$iface" 2>/dev/null || true
}

[[ $(uname -s) == Darwin ]] || die "this script is for macOS; on Linux see the systemd unit in agent-bridge/README.md"
[[ $EUID -ne 0 ]] || die "run as your own user, not with sudo: agent-bridge must run next to your herdr server"
[[ -f "$REPO_DIR/agent-bridge/pyproject.toml" ]] || die "run from a checkout of claude-remote-mcp"
command -v uv >/dev/null || die "uv not found; install it: https://docs.astral.sh/uv/getting-started/installation/"

DEFAULT_HOST="$(installed ProgramArguments:4)"
ask HOST "LAN address to listen on" "${DEFAULT_HOST:-$(lan_address)}"
[[ $HOST != 0.0.0.0 ]] || die "listen on the LAN address, not 0.0.0.0"
if [[ $HOST != 127.0.0.1 ]] && ! ifconfig | grep -q "inet $HOST "; then
  echo "warning: no interface has $HOST right now; agent-bridge retries every 10 s until one does"
fi
DEFAULT_PORT="$(installed ProgramArguments:6)"
ask PORT "Port" "${DEFAULT_PORT:-8765}"
[[ $PORT =~ ^[0-9]+$ ]] || die "PORT must be a number"

DEFAULT_ROOTS="$(installed EnvironmentVariables:AGENT_BRIDGE_ALLOWED_ROOTS)"
ask ALLOWED_ROOTS "Folders agents may start in, :-separated" "${DEFAULT_ROOTS:-$HOME/Projects}"
ROOTS=""
IFS=: read -ra ROOT_LIST <<<"$ALLOWED_ROOTS"
for ROOT in "${ROOT_LIST[@]}"; do
  [[ -n $ROOT ]] || continue
  absolute ROOT ALLOWED_ROOTS
  [[ -d $ROOT ]] || echo "warning: $ROOT does not exist yet"
  ROOTS="${ROOTS:+$ROOTS:}$ROOT"
done
[[ -n $ROOTS ]] || die "ALLOWED_ROOTS needs at least one folder"

DEFAULT_HERDR="$(installed EnvironmentVariables:AGENT_BRIDGE_HERDR_BIN)"
ask HERDR_BIN "Full path to herdr" "${DEFAULT_HERDR:-$(command -v herdr || true)}"
absolute HERDR_BIN
[[ -x $HERDR_BIN ]] || die "$HERDR_BIN is not an executable"

if [[ -z ${AGENT_BRIDGE_CLAUDE_CONFIG_DIR+x} || -z ${TRUST_ROOT+x} ]]; then
  cat <<'EOF'

Folder trust (optional). The first time Claude opens in a folder it asks "Do you trust
this folder?", and an agent started remotely would quit there. agent-bridge can mark
folders under a trust root as trusted beforehand, but only in a separate Claude config
for the agents, never in your own. Answer - to skip this if you have already trusted your
project folders (or a folder above them) by opening claude there.
EOF
fi
# Suggest a new config, unless an earlier run answered this already, even with none.
DEFAULT_CONFIG_DIR="$HOME/.claude-agents"
[[ ! -f $PLIST ]] || DEFAULT_CONFIG_DIR="$(installed EnvironmentVariables:AGENT_BRIDGE_CLAUDE_CONFIG_DIR)"
ask_optional AGENT_BRIDGE_CLAUDE_CONFIG_DIR "Separate Claude config folder for agents" "$DEFAULT_CONFIG_DIR"
absolute AGENT_BRIDGE_CLAUDE_CONFIG_DIR
if [[ -z $AGENT_BRIDGE_CLAUDE_CONFIG_DIR && -z ${TRUST_ROOT+x} ]]; then
  TRUST_ROOT=""
else
  # Suggest the folder agents start in, unless an earlier run answered this already
  # (it did if it set a config dir), even with none.
  DEFAULT_TRUST_ROOT="${ROOTS%%:*}"
  [[ -z $(installed EnvironmentVariables:AGENT_BRIDGE_CLAUDE_CONFIG_DIR) ]] \
    || DEFAULT_TRUST_ROOT="$(installed EnvironmentVariables:AGENT_BRIDGE_TRUST_ROOT)"
  ask_optional TRUST_ROOT "Trust agents' folders under" "$DEFAULT_TRUST_ROOT"
fi
absolute TRUST_ROOT
[[ -z $TRUST_ROOT || -n $AGENT_BRIDGE_CLAUDE_CONFIG_DIR ]] \
  || die "TRUST_ROOT needs AGENT_BRIDGE_CLAUDE_CONFIG_DIR: folders are only trusted in a separate Claude config"
LOGIN_HINT=""
if [[ -n $AGENT_BRIDGE_CLAUDE_CONFIG_DIR && ! -f $AGENT_BRIDGE_CLAUDE_CONFIG_DIR/.claude.json ]]; then
  LOGIN_HINT="Log in to Claude once in the agents' config, or new agents can't start:
  CLAUDE_CONFIG_DIR=$AGENT_BRIDGE_CLAUDE_CONFIG_DIR claude"
  echo "warning: $AGENT_BRIDGE_CLAUDE_CONFIG_DIR has no Claude login yet (see the end)"
fi

# Never asked for (it's JSON); passed in, or kept from the installed settings.
ACCOUNTS="${AGENT_BRIDGE_ACCOUNTS-$(installed EnvironmentVariables:AGENT_BRIDGE_ACCOUNTS)}"

# A token passed in or typed wins; otherwise keep the installed one, unless rotating.
INSTALLED_TOKEN="$(installed EnvironmentVariables:AGENT_BRIDGE_TOKEN)"
TOKEN="${AGENT_BRIDGE_TOKEN:-}"
if [[ -z $TOKEN && -z ${ROTATE_TOKEN:-} ]]; then
  TOKEN_HINT="Enter generates a new one"
  [[ -z $INSTALLED_TOKEN ]] || TOKEN_HINT="Enter keeps the installed one"
  echo
  read -rsp "Token, hidden as you type or paste [$TOKEN_HINT]: " TOKEN || true
  echo
fi
TOKEN_SOURCE=given
if [[ -n $TOKEN ]]; then
  [[ -z ${ROTATE_TOKEN:-} ]] || die "set AGENT_BRIDGE_TOKEN or ROTATE_TOKEN, not both"
  [[ ${#TOKEN} -ge 32 ]] || die "the token must be at least 32 characters"
  # The characters a bearer token may have (RFC 6750), as mcp-gateway's setup.sh checks.
  [[ $TOKEN =~ ^[A-Za-z0-9._~+/-]+=*$ ]] \
    || die "the token may only contain letters, digits, . _ ~ + / - and trailing ="
elif [[ -z ${ROTATE_TOKEN:-} ]]; then
  TOKEN="$INSTALLED_TOKEN"
  TOKEN_SOURCE=kept
fi
if [[ -z $TOKEN ]]; then
  TOKEN="$(openssl rand -hex 32)"
  TOKEN_SOURCE=new
fi

# --- Install -------------------------------------------------------------------------

log "Installing dependencies into $REPO_DIR/.venv"
(cd "$REPO_DIR" && uv sync --all-packages)

log "Writing $PLIST"
xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
env_entry() { # env_entry NAME VALUE: one EnvironmentVariables entry, none when empty
  [[ -z $2 ]] || printf '    <key>%s</key><string>%s</string>\n' "$1" "$(xml "$2")"
}
ENV_ENTRIES="$(
  env_entry AGENT_BRIDGE_TOKEN "$TOKEN"
  env_entry AGENT_BRIDGE_ALLOWED_ROOTS "$ROOTS"
  env_entry AGENT_BRIDGE_HERDR_BIN "$HERDR_BIN"
  env_entry AGENT_BRIDGE_CLAUDE_CONFIG_DIR "$AGENT_BRIDGE_CLAUDE_CONFIG_DIR"
  env_entry AGENT_BRIDGE_TRUST_ROOT "$TRUST_ROOT"
  env_entry AGENT_BRIDGE_ACCOUNTS "$ACCOUNTS"
)"
mkdir -p "$(dirname "$PLIST")"
# mktemp makes the file 0600: it holds the token.
TMP_PLIST="$(mktemp "$PLIST.XXXXXX")"
cat >"$TMP_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml "$REPO_DIR")/.venv/bin/uvicorn</string>
    <string>agent_bridge.app:main</string>
    <string>--factory</string>
    <string>--host</string><string>$(xml "$HOST")</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
$ENV_ENTRIES
  </dict>
  <key>RunAtLoad</key><true/>
  <!-- Retries until the LAN address exists, e.g. right after login or wake. -->
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <!-- Errors (stderr) and uvicorn's one line per request (stdout), in one file. -->
  <key>StandardOutPath</key><string>$(xml "$LOG_FILE")</string>
  <key>StandardErrorPath</key><string>$(xml "$LOG_FILE")</string>
</dict>
</plist>
EOF
plutil -lint -s "$TMP_PLIST" || { rm -f "$TMP_PLIST"; die "the generated plist is invalid"; }
mv "$TMP_PLIST" "$PLIST"

# --- Start and check -----------------------------------------------------------------

log "Starting $LABEL"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl enable "$DOMAIN/$LABEL"
# bootout returns before the old job is gone, so the first bootstrap can fail.
for attempt in $(seq 10); do
  launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null && break
  [[ $attempt -lt 10 ]] || die "launchctl bootstrap failed; see: launchctl print $DOMAIN/$LABEL"
  sleep 1
done

check() { # check "label" expected-status curl-args...
  local label=$1 expected=$2; shift 2
  local status
  status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true)"
  if [[ $status == "$expected" ]]; then echo "ok    $label ($status)"; else echo "FAIL  $label (got $status, want $expected)"; fi
}

URL="http://$HOST:$PORT"
log "Checks"
for _ in $(seq 30); do curl -s -o /dev/null --max-time 2 "$URL/agents" && break; sleep 0.5; done
check "agent-bridge refuses requests without the token" 401 "$URL/agents"
check "agent-bridge lists herdr agents with its token" 200 -H "Authorization: Bearer $TOKEN" "$URL/agents"

cat <<EOF

Done. agent-bridge listens on $URL and starts at every login.
If a check failed: tail $LOG_FILE (a 502 means the herdr server isn't running).
If the macOS firewall asks, allow incoming connections for Python.
${LOGIN_HINT:+
$LOGIN_HINT
}

For mcp-gateway's setup.sh on the gateway host:
  AGENT_BRIDGE_URL=$URL
  PROJECTS_ROOT=${ROOTS%%:*}   (or another folder under $ROOTS)
EOF
case $TOKEN_SOURCE in
  new) echo "  AGENT_BRIDGE_TOKEN=$TOKEN   (new: re-run mcp-gateway's setup.sh with it)" ;;
  given) echo "  AGENT_BRIDGE_TOKEN is the one you passed in" ;;
  kept) echo "  AGENT_BRIDGE_TOKEN is unchanged; read it with:"
        echo "    /usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:AGENT_BRIDGE_TOKEN' $PLIST" ;;
esac
