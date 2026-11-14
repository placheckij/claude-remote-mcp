#!/usr/bin/env bash
# Installs agent-bridge as a systemd user service on Linux and enrolls it with the gateway:
# it runs from this checkout, starts at boot (lingering, so without logging in) and restarts
# when it stops. herdr's server gets a user service too, so agents can start after a reboot.
# Safe to re-run, e.g. after pulling new code: it asks only for what's missing, keeps the
# token, and never restarts herdr's server, so running agents stay.
#
# Run as your own user (not sudo), from a checkout of this repo on the agent host, with
# `sudo mcp-gateway enroll` run on the gateway host first:
#   agent-bridge/deploy/linux/setup.sh
# Missing values are asked for; pass them in to skip the questions or to change one:
#   ALLOWED_ROOTS=/home/me/projects NAME=linux-box ENROLLMENT='crm1@…' agent-bridge/deploy/linux/setup.sh
# ALLOWED_ROOTS: the `:`-separated folders agents may start in. NAME: this host's name in
# agent ids (the hostname by default; once enrolled, the gateway's name, renamed with
# `sudo mcp-gateway bridges rename` on the gateway host). ENROLLMENT: what `sudo mcp-gateway enroll` printed,
# empty to enroll later. Optional: HOST (found: the LAN address), PORT (8765), HERDR_BIN
# (found), AGENT_BRIDGE_CLAUDE_CONFIG_DIR and TRUST_ROOT (folder trust, see
# agent-bridge/README.md; empty for none), AGENT_BRIDGE_ACCOUNTS (named Claude logins as
# JSON; empty removes them), ROTATE_TOKEN=1 (a new token; needs ENROLLMENT, so the gateway
# learns it), HERDR_SERVICE=0 (no service for herdr's server: you start it yourself). The
# settings go in the env file below, and its other lines are kept.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$DEPLOY_DIR/../../.." && pwd)"
AGENT_BRIDGE="$REPO_DIR/.venv/bin/agent-bridge"
UNIT=agent-bridge.service
HERDR_UNIT=agent-bridge-herdr.service
UNIT_DIR="$HOME/.config/systemd/user"
# Where `agent-bridge serve` reads them, as systemd starts it with no settings.
ENV_FILE="$HOME/.config/agent-bridge/env"
STATE_FILE="$HOME/.local/state/agent-bridge/state.json"
# The AGENT_BRIDGE_* settings this script writes.
MANAGED="TOKEN|ALLOWED_ROOTS|HOST|PORT|NAME|HERDR_BIN|CLAUDE_CONFIG_DIR|TRUST_ROOT|ACCOUNTS"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
trap 'rm -f "${TMP_ENV:-}" "${TMP_UNIT:-}"' EXIT

ask() { # ask VAR "question" [default] [hint]: Enter, or no input left, takes the default
  local var=$1 answer=""
  printf '%s [%s]: ' "$2" "${4:-${3:-Enter to skip}}" >&2
  IFS= read -r answer || true
  printf -v "$var" '%s' "${answer:-${3:-}}"
}

ask_optional() { # like ask, but "-" answers none
  local var=$1 answer="" hint="Enter to skip"
  [[ -z ${3:-} ]] || hint="$3; Enter for that, - for none"
  printf '%s [%s]: ' "$2" "$hint" >&2
  IFS= read -r answer || true
  answer=${answer:-${3:-}}
  [[ $answer != - ]] || answer=""
  printf -v "$var" '%s' "$answer"
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

bridge() { # the venv's agent-bridge, reading only the files this script writes
  (
    for var in $(compgen -v AGENT_BRIDGE_); do unset "$var"; done
    exec "$AGENT_BRIDGE" "$@"
  )
}

installed() { # installed KEY: the installed AGENT_BRIDGE_KEY, or nothing
  local value=""
  [[ -f $ENV_FILE ]] || return 0
  # With or without `export ` in front, as the loader reads it.
  value="$(sed -nE "s/^(export[[:space:]]+)?AGENT_BRIDGE_$1=//p" "$ENV_FILE" | tail -n 1)"
  case $value in
    # Single-quoted by line(), which doubles backslashes for the loader.
    \'*\') value=${value#\'}; value=${value%\'}; value="$(printf '%s' "$value" | sed 's/\\\\/\\/g')" ;;
    \"*\") value=${value#\"}; value=${value%\"} ;;
  esac
  printf '%s' "$value"
}

answered() { # answered KEY: whether an earlier run set KEY, even to nothing
  [[ -f $ENV_FILE ]] && grep -Eq "^(export[[:space:]]+)?AGENT_BRIDGE_$1=" "$ENV_FILE"
}

pick() { # pick VAR KEY: VAR passed in, else the installed KEY; fails when that is empty
  [[ -n ${!1+x} ]] || printf -v "$1" '%s' "$(installed "$2")"
  [[ -n ${!1} ]]
}

has_address() { [[ $1 == 127.0.0.1 ]] || ip -4 -o addr show 2>/dev/null | grep -q "inet $1/"; }

lan_address() { # the source address of the default route, or nothing; nothing is sent
  ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1 || true
}

find_herdr() { # the installed herdr, else the one on PATH, else one in the usual places
  local path
  for path in "$(installed HERDR_BIN)" "$(command -v herdr || true)" \
    "$HOME/.local/bin/herdr" /usr/local/bin/herdr /usr/bin/herdr; do
    if [[ -n $path && -x $path ]]; then echo "$path"; return 0; fi
  done
  return 0
}

listener() { # listener PORT: the PID listening on PORT, "?" when its PID can't be seen
  local found
  if command -v ss >/dev/null; then
    found="$(ss -Hltnp "sport = :$1" 2>/dev/null || true)"
    [[ -n $found ]] || return 0
    # ss shows the PID of your own processes only.
    found="$(grep -o 'pid=[0-9]*' <<<"$found" | head -n 1 || true)"
    found=${found#pid=}
    echo "${found:-?}"
  else
    lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true
  fi
}

command_of() { ps -o args= -p "$1" 2>/dev/null || true; }

default_config() { # default_config DIR: whether DIR is Claude's own config, which agent-bridge refuses
  local dir home
  dir="$(realpath -m "$1")"
  home="$(realpath -m "$HOME")"
  [[ $dir == "$home" || $dir == "$home/.claude" || $dir == "$home/.claude/"* \
    || $dir/.claude.json -ef $home/.claude.json ]]
}

unit_path() { # unit_path PATH: refuses what systemd can't run, even quoted
  [[ $1 != *[\"\\]* ]] || die "systemd can't run a program whose path has \" or \\ in it: $1"
}

holder() { # holder PID: "service" (this unit), "by-hand" (agent-bridge outside it), "other"
  local pattern='agent-bridge serve|agent_bridge'
  if [[ $1 != "?" ]]; then
    [[ $1 != "$(systemctl --user show -p MainPID --value "$UNIT" 2>/dev/null || true)" ]] \
      || { echo service; return; }
    [[ ! $(command_of "$1") =~ $pattern ]] || { echo by-hand; return; }
  fi
  echo other
}

herdr_running() { "$HERDR_BIN" status server 2>/dev/null | grep -q '^status: running'; }

enrolled() { # enrolled KEY: the gateway's KEY (name, url) from the state file, or nothing
  [[ -f $STATE_FILE ]] || return 0
  tr -d '\n' <"$STATE_FILE" | sed -n "s/.*\"gateway\": *{[^}]*\"$1\": *\"\([^\"]*\)\".*/\1/p"
}

# 0 is off, as for HERDR_SERVICE.
[[ ${ROTATE_TOKEN:-0} != 0 ]] || ROTATE_TOKEN=""
[[ $(uname -s) == Linux ]] || die "this script is for Linux; on macOS run agent-bridge/deploy/setup-macos.sh"
[[ $EUID -ne 0 ]] || die "run as your own user, not with sudo: agent-bridge must run next to your herdr server"
[[ -f "$REPO_DIR/agent-bridge/pyproject.toml" ]] || die "run from a checkout of claude-remote-mcp"
unit_path "$AGENT_BRIDGE"
command -v uv >/dev/null || die "uv not found; install it: https://docs.astral.sh/uv/getting-started/installation/"
systemctl --user show-environment >/dev/null 2>&1 \
  || die "can't reach your systemd user manager; run this from your own login (SSH or a console), not through su or sudo -u"

log "Installing dependencies into $REPO_DIR/.venv"
(cd "$REPO_DIR" && uv sync --all-packages)
[[ -x $AGENT_BRIDGE ]] || die "uv sync didn't install $AGENT_BRIDGE"

# --- Settings: passed in, else installed, else asked ---------------------------------

pick ALLOWED_ROOTS ALLOWED_ROOTS \
  || ask ALLOWED_ROOTS "Folders agents may start in, :-separated" "$HOME/Projects"
ROOTS=""
IFS=: read -ra ROOT_LIST <<<"$ALLOWED_ROOTS"
for ROOT in "${ROOT_LIST[@]}"; do
  [[ -n $ROOT ]] || continue
  absolute ROOT ALLOWED_ROOTS
  [[ -d $ROOT ]] || echo "warning: $ROOT does not exist yet"
  ROOTS="${ROOTS:+$ROOTS:}$ROOT"
done
[[ -n $ROOTS ]] || die "ALLOWED_ROOTS needs at least one folder"

# uname, as some distributions don't install hostname.
DEFAULT_NAME="$(uname -n | cut -d. -f1 | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"
# The bridge saves the gateway's name for it when it asks, at its start or a check. Asked
# here, with the installed token, so a rename done there since then is seen before a new
# token (which the gateway doesn't know yet) is written or an enroll sends the old name.
[[ -z $(enrolled name) || ! -f $ENV_FILE ]] || bridge check >/dev/null 2>&1 || true
ENROLLED_NAME="$(enrolled name)"
NAME_GIVEN=${NAME+1}
pick NAME NAME || {
  ask NAME "This host's name in agent ids, like $DEFAULT_NAME/w1:p1" "${ENROLLED_NAME:-${DEFAULT_NAME:-agent-host}}"
  NAME_GIVEN=1
}
until bridge check --name-format "$NAME"; do
  ask NAME "This host's name" "" "required"
  [[ -n $NAME ]] || die "NAME is required"
done
# Once enrolled, the gateway's name is this host's name: hosts are renamed there, and the
# installed NAME follows.
WANTED_NAME=""
if [[ -n $ENROLLED_NAME && $ENROLLED_NAME != "$NAME" ]]; then
  [[ -z $NAME_GIVEN ]] || WANTED_NAME=$NAME
  NAME=$ENROLLED_NAME
fi

# The installed address while this host still has it; otherwise its current one.
if [[ -z ${HOST+x} ]]; then
  INSTALLED_HOST="$(installed HOST)"
  if [[ -n $INSTALLED_HOST ]] && has_address "$INSTALLED_HOST"; then
    HOST=$INSTALLED_HOST
  else
    HOST="$(lan_address)"
    [[ -z $INSTALLED_HOST || -z $HOST ]] || echo "The LAN address is now $HOST (was $INSTALLED_HOST)."
  fi
fi
[[ -n $HOST ]] || die "couldn't find this host's LAN address; pass it, e.g. HOST=192.168.1.20"
[[ $HOST != 0.0.0.0 ]] || die "listen on the LAN address, not 0.0.0.0"
has_address "$HOST" || echo "warning: no interface has $HOST right now; agent-bridge retries every 10 s until one does"

pick PORT PORT || PORT=8765
[[ $PORT =~ ^[0-9]+$ ]] || die "PORT must be a number"
# A port in use is fine only when this service holds it, i.e. the version being replaced.
while PID="$(listener "$PORT")"; [[ -n $PID ]]; do
  case "$(holder "$PID")" in
    service) break ;;
    by-hand)
      # Taking it over would leave the service failing to bind, every 10 s.
      echo "Port $PORT is held by an agent-bridge running outside the service, PID $PID: $(command_of "$PID")"
      printf 'Stop it (Ctrl-C where it runs), then press Enter: ' >&2
      read -r _ || die "stop agent-bridge (PID $PID), then run this again" ;;
    *)
      if [[ $PID == "?" ]]; then
        echo "Port $PORT is in use by another user's program."
      else
        echo "Port $PORT is in use by PID $PID: $(command_of "$PID")"
      fi
      FREE=$((PORT + 1))
      while [[ -n $(listener "$FREE") ]]; do FREE=$((FREE + 1)); done
      ask PORT "Port" "$FREE"
      [[ $PORT =~ ^[0-9]+$ ]] || die "PORT must be a number" ;;
  esac
done

if [[ -z ${HERDR_BIN+x} ]]; then
  HERDR_BIN="$(find_herdr)"
  [[ -n $HERDR_BIN ]] || die "herdr not found; install it, or pass HERDR_BIN=/full/path/to/herdr"
fi
absolute HERDR_BIN
[[ -x $HERDR_BIN ]] || die "$HERDR_BIN is not an executable"
[[ ${HERDR_SERVICE:-1} == 0 ]] || unit_path "$HERDR_BIN"

# Folder trust is asked once; later runs keep the answer, even none.
if [[ -z ${AGENT_BRIDGE_CLAUDE_CONFIG_DIR+x} ]] && answered CLAUDE_CONFIG_DIR; then
  AGENT_BRIDGE_CLAUDE_CONFIG_DIR="$(installed CLAUDE_CONFIG_DIR)"
fi
if [[ -z ${TRUST_ROOT+x} ]] && answered TRUST_ROOT; then
  TRUST_ROOT="$(installed TRUST_ROOT)"
fi
if [[ -z ${AGENT_BRIDGE_CLAUDE_CONFIG_DIR+x} || -z ${TRUST_ROOT+x} ]]; then
  cat <<'EOF'

Folder trust (optional). The first time Claude opens in a folder it asks "Do you trust
this folder?", and an agent started remotely would quit there. agent-bridge can mark
folders under a trust root as trusted beforehand, but only in a separate Claude config
for the agents, never in your own. Answer - to skip this if you have already trusted your
project folders (or a folder above them) by opening claude there.
EOF
fi
[[ -n ${AGENT_BRIDGE_CLAUDE_CONFIG_DIR+x} ]] \
  || ask_optional AGENT_BRIDGE_CLAUDE_CONFIG_DIR "Separate Claude config folder for agents" "$HOME/.claude-agents"
absolute AGENT_BRIDGE_CLAUDE_CONFIG_DIR
[[ -z $AGENT_BRIDGE_CLAUDE_CONFIG_DIR ]] || ! default_config "$AGENT_BRIDGE_CLAUDE_CONFIG_DIR" \
  || die "$AGENT_BRIDGE_CLAUDE_CONFIG_DIR is your own Claude config; the agents need a separate one, e.g. $HOME/.claude-agents"
if [[ -z ${TRUST_ROOT+x} ]]; then
  if [[ -z $AGENT_BRIDGE_CLAUDE_CONFIG_DIR ]]; then
    TRUST_ROOT=""
  else
    ask_optional TRUST_ROOT "Trust agents' folders under" "${ROOTS%%:*}"
  fi
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
ACCOUNTS="${AGENT_BRIDGE_ACCOUNTS-$(installed ACCOUNTS)}"

# Kept, unless rotating. Nobody copies it: enrolling hands it to the gateway.
TOKEN="$(installed TOKEN)"
OLD_TOKEN=$TOKEN
NEW_TOKEN=""
if [[ -n ${ROTATE_TOKEN:-} || -z $TOKEN ]]; then
  TOKEN="$(openssl rand -hex 32)"
  NEW_TOKEN=1
fi
# Checked here, not by agent-bridge: on its command line, ps would show the token.
[[ ${#TOKEN} -ge 32 && $TOKEN =~ ^[A-Za-z0-9._~+/-]+=*$ ]] \
  || die "the installed token won't do; run again with ROTATE_TOKEN=1"

# Asked for until this host is enrolled, and again with a new token.
if [[ -z ${ENROLLMENT+x} ]] && [[ -z $ENROLLED_NAME || -n $NEW_TOKEN ]]; then
  [[ -z $ENROLLED_NAME || -z $NEW_TOKEN || -n ${ROTATE_TOKEN:-} ]] \
    || printf '\nThe env file had no token, so this host has a new one; enroll again so the gateway learns it.\n'
  printf '\nTo enroll with the gateway, run on the gateway host: sudo mcp-gateway enroll\n'
  ask ENROLLMENT "Enrollment string it printed, crm1@…" "" "Enter to enroll later"
fi
ENROLLMENT=${ENROLLMENT:-}
[[ -z ${ROTATE_TOKEN:-} || -n $ENROLLMENT ]] \
  || die "ROTATE_TOKEN needs an enrollment string, so the gateway learns the new token: run sudo mcp-gateway enroll on the gateway host and pass what it prints as ENROLLMENT"

# --- Install -------------------------------------------------------------------------

line() { # line KEY VALUE: one env file line, single-quoted so nothing in it is expanded
  [[ $2 != *"'"* && $2 != *$'\n'* ]] || die "AGENT_BRIDGE_$1 can't contain ' or a line break"
  # The loader reads \\ in single quotes as one backslash.
  printf "AGENT_BRIDGE_%s='%s'\n" "$1" "$(printf '%s' "$2" | sed 's/\\/\\\\/g')"
}

write_env() {
  log "Writing $ENV_FILE"
  mkdir -p "$(dirname "$ENV_FILE")"
  chmod 700 "$(dirname "$ENV_FILE")"
  # mktemp makes the file 0600: it holds the token.
  TMP_ENV="$(mktemp "$ENV_FILE.XXXXXX")"
  {
    line TOKEN "$TOKEN"
    line ALLOWED_ROOTS "$ROOTS"
    line HOST "$HOST"
    line PORT "$PORT"
    line NAME "$NAME"
    line HERDR_BIN "$HERDR_BIN"
    # Written even when empty, so the next run knows folder trust was answered.
    line CLAUDE_CONFIG_DIR "$AGENT_BRIDGE_CLAUDE_CONFIG_DIR"
    line TRUST_ROOT "$TRUST_ROOT"
    [[ -z $ACCOUNTS ]] || line ACCOUNTS "$ACCOUNTS"
    [[ ! -f $ENV_FILE ]] || grep -Ev "^(export[[:space:]]+)?AGENT_BRIDGE_($MANAGED)=" "$ENV_FILE" || true
  } >"$TMP_ENV"
  mv "$TMP_ENV" "$ENV_FILE"
}

write_unit() { # write_unit NAME PLACEHOLDER VALUE: the template with VALUE filled in
  log "Writing $UNIT_DIR/$1"
  mkdir -p "$UNIT_DIR"
  # The template double-quotes it (unit_path refused " and \): % is escaped as a systemd
  # specifier, then the sed replacement for its | delimiter.
  local value
  value="$(printf '%s' "$3" | sed -e 's/%/%%/g' -e 's/[\\&|]/\\&/g')"
  TMP_UNIT="$(mktemp "$UNIT_DIR/$1.XXXXXX")"
  sed -e "s|@$2@|$value|" "$DEPLOY_DIR/$1" >"$TMP_UNIT"
  chmod 644 "$TMP_UNIT"
  mv "$TMP_UNIT" "$UNIT_DIR/$1"
}

start_herdr() {
  if [[ ${HERDR_SERVICE:-1} == 0 ]]; then
    if [[ -f $UNIT_DIR/$HERDR_UNIT ]]; then
      log "Removing $HERDR_UNIT (its server keeps running until it stops)"
      systemctl --user disable --quiet "$HERDR_UNIT" 2>/dev/null || true
      rm -f "$UNIT_DIR/$HERDR_UNIT"
      systemctl --user daemon-reload
    fi
    return 0
  fi
  systemctl --user enable --quiet "$HERDR_UNIT"
  # Never restarted: that would stop every agent. A changed unit applies at its next start.
  if systemctl --user is-active --quiet "$HERDR_UNIT"; then
    echo "herdr's server runs as $HERDR_UNIT."
  elif herdr_running; then
    echo "herdr's server already runs outside systemd; $HERDR_UNIT starts it from the next boot."
  else
    log "Starting herdr's server ($HERDR_UNIT)"
    systemctl --user start "$HERDR_UNIT"
  fi
}

start_service() {
  log "Starting $UNIT"
  systemctl --user enable --quiet "$UNIT"
  systemctl --user restart "$UNIT"
  log "Checks"
  local result
  for _ in $(seq 30); do
    result="$(bridge check 2>&1 || true)"
    [[ $result == *"FAIL  agent-bridge answers"* ]] || break
    sleep 0.5
  done
  printf '%s\n' "$result"
}

write_env
write_unit "$UNIT" AGENT_BRIDGE "$AGENT_BRIDGE"
[[ ${HERDR_SERVICE:-1} == 0 ]] || write_unit "$HERDR_UNIT" HERDR_BIN "$HERDR_BIN"
systemctl --user daemon-reload
start_herdr

# Lingering starts the user's services at boot and keeps them after logout.
LINGER_HINT=""
if [[ $(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true) != yes ]]; then
  log "Turning on lingering, so the services run without a login"
  loginctl enable-linger "$(id -un)" 2>/dev/null \
    || LINGER_HINT="Lingering is off, so agent-bridge stops at your last logout and doesn't start at boot. Turn it on with:
  sudo loginctl enable-linger $(id -un)"
fi

start_service

# --- Enroll --------------------------------------------------------------------------

ENROLLED=""
TAKEN=""
while [[ -n $ENROLLMENT ]]; do
  log "Enrolling with the gateway"
  status=0
  bridge enroll "$ENROLLMENT" || status=$?
  case $status in
    0) ENROLLED=1; break ;;
    3) # The gateway has another host by that name.
      TAKEN=$NAME
      NAME=""
      until [[ -n $NAME ]] && bridge check --name-format "$NAME"; do
        ask NAME "Another name for this host" "" "required"
        [[ -n $NAME ]] || break
      done
      # No other name: stop below, once a rotated token is put back.
      if [[ -z $NAME ]]; then NAME=$TAKEN; break; fi
      TAKEN=""
      write_env
      start_service ;;
    4) break ;; # agent-bridge printed the command to run once the gateway is up
    *)
      ask ENROLLMENT "Enrollment string, crm1@…" "" "Enter to enroll later" ;;
  esac
done

# The gateway still has the old token: put it back, or the gateway's calls are refused.
if [[ -n ${ROTATE_TOKEN:-} && -z $ENROLLED && -n $OLD_TOKEN && -n $(enrolled name) ]]; then
  log "Putting the old token back: the gateway didn't learn the new one"
  TOKEN=$OLD_TOKEN
  # And its name for this host, should another one have been given for a taken name.
  NAME="$(enrolled name)"
  write_env
  start_service
  die "the token was not rotated${TAKEN:+, as the gateway has another host named $TAKEN}; run again with ROTATE_TOKEN=1 and a new string from sudo mcp-gateway enroll"
fi
[[ -z $TAKEN ]] || die "the gateway has another host by that name; run again with another NAME"

cat <<EOF

Done. agent-bridge listens on http://$HOST:$PORT and starts at boot.
If a check failed: journalctl --user -u $UNIT (herdr failing means its server isn't running).
${LINGER_HINT:+
$LINGER_HINT
}${LOGIN_HINT:+
$LOGIN_HINT
}
EOF
if [[ -n $(enrolled name) ]]; then
  echo "It is enrolled with the gateway at $(enrolled url) as $(enrolled name): Claude reaches its agents as $(enrolled name)/…"
  [[ -z $WANTED_NAME || $WANTED_NAME == "$(enrolled name)" ]] \
    || echo "The gateway names this host $(enrolled name), so NAME stays that. To rename it $WANTED_NAME, run on the gateway host: sudo mcp-gateway bridges rename $(enrolled name) $WANTED_NAME"
  [[ -z $NEW_TOKEN || -n $ENROLLED ]] \
    || echo "The gateway has this host's old token and its calls will be refused: run sudo mcp-gateway enroll on the gateway host, then run this again with ENROLLMENT='<what it printed>'"
else
  echo "It is not enrolled with a gateway yet. Run sudo mcp-gateway enroll on the gateway host, then here:"
  echo "  $AGENT_BRIDGE enroll '<what it printed>'"
fi
