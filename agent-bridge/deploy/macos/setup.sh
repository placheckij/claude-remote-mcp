#!/usr/bin/env bash
# Installs agent-bridge as a launchd agent on macOS and enrolls it with the gateway: it runs
# from this checkout, starts at login and restarts when it stops. Safe to re-run, e.g. after
# pulling new code: it asks only for what's missing and keeps the token.
#
# Run as your own user (not sudo), from a checkout of this repo on the agent host, with
# `sudo mcp-gateway enroll` run on the gateway host first:
#   agent-bridge/deploy/macos/setup.sh
# Missing values are asked for; pass them in to skip the questions or to change one:
#   ALLOWED_ROOTS=/Users/me/projects NAME=mac ENROLLMENT='crm1@…' agent-bridge/deploy/macos/setup.sh
# ALLOWED_ROOTS: the `:`-separated folders agents may start in. NAME: this host's name in
# agent ids (the hostname by default). ENROLLMENT: what `sudo mcp-gateway enroll` printed,
# empty to enroll later. Optional: HOST (found: the LAN address), PORT (8765), HERDR_BIN
# (found), AGENT_BRIDGE_CLAUDE_CONFIG_DIR and TRUST_ROOT (folder trust, see
# agent-bridge/README.md; empty for none), AGENT_BRIDGE_ACCOUNTS (named Claude logins as
# JSON; empty removes them), ROTATE_TOKEN=1 (a new token; needs ENROLLMENT, so the gateway
# learns it). The settings go in the env file below; its other settings are kept, but not
# its comments.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$DEPLOY_DIR/../../.." && pwd)"
AGENT_BRIDGE="$REPO_DIR/.venv/bin/agent-bridge"
LABEL=dev.claude-remote.agent-bridge
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_FILE="$HOME/Library/Logs/agent-bridge.log"
# Where `agent-bridge serve` reads them, as launchd starts it with no environment.
SUPPORT_DIR="$HOME/Library/Application Support/agent-bridge"
ENV_FILE="$SUPPORT_DIR/env"
STATE_FILE="$SUPPORT_DIR/state.json"
DOMAIN="gui/$(id -u)"
# The AGENT_BRIDGE_* settings this script writes.
MANAGED="TOKEN|ALLOWED_ROOTS|HOST|PORT|NAME|HERDR_BIN|CLAUDE_CONFIG_DIR|TRUST_ROOT|ACCOUNTS"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
trap 'rm -f "${TMP_ENV:-}" "${TMP_PLIST:-}"' EXIT

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
  # Through '%s': bash 3.2's printf -v leaves the variable as it was on an empty format.
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

plist_value() { # plist_value KEY: a value from the installed plist, or nothing
  [[ -f $PLIST ]] && /usr/libexec/PlistBuddy -c "Print :$1" "$PLIST" 2>/dev/null || true
}

# Installed settings come from the env file, or on the first run of this script from the
# plist an earlier version wrote them to.
if [[ -f $ENV_FILE ]]; then
  SOURCE=env_file
elif [[ -n $(plist_value EnvironmentVariables:AGENT_BRIDGE_TOKEN) ]]; then
  SOURCE=plist
else
  SOURCE=none
fi

dotenv() { # dotenv get|has|others ARG: the env file as agent-bridge reads it (python-dotenv,
  # keys in any case, the last one winning)
  "$REPO_DIR/.venv/bin/python" -I - "$ENV_FILE" "$@" <<'EOF'
import re, sys
from dotenv import dotenv_values
path, mode, arg = sys.argv[1:4]
values = {key.upper(): value for key, value in dotenv_values(path).items()}
if mode == "get":
    sys.stdout.write(values.get(arg) or "")
elif mode == "has":
    sys.exit(0 if arg in values else 1)
else:  # others: the settings not matching ARG, as NUL-separated names and values
    for key, value in values.items():
        if value is not None and not re.fullmatch(arg, key):
            sys.stdout.write(f"{key}\0{value}\0")
EOF
}

installed() { # installed KEY: the installed AGENT_BRIDGE_KEY, or nothing
  local value=""
  case $SOURCE in
    env_file) value="$(dotenv get "AGENT_BRIDGE_$1")" ;;
    plist)
      # It ran uvicorn with --host HOST --port PORT, and had the rest in its environment.
      case $1 in
        HOST) value="$(plist_value ProgramArguments:4)" ;;
        PORT) value="$(plist_value ProgramArguments:6)" ;;
        *) value="$(plist_value "EnvironmentVariables:AGENT_BRIDGE_$1")" ;;
      esac ;;
  esac
  printf '%s' "$value"
}

answered() { # answered KEY: whether an earlier run set KEY, even to nothing
  case $SOURCE in
    env_file) dotenv has "AGENT_BRIDGE_$1" ;;
    plist) true ;; # the earlier version asked every question, and left empty answers out
    *) false ;;
  esac
}

pick() { # pick VAR KEY: VAR passed in, else the installed KEY; fails when that is empty
  [[ -n ${!1+x} ]] || printf -v "$1" '%s' "$(installed "$2")"
  [[ -n ${!1} ]]
}

canonical() { # canonical ADDRESS: an IP address as ifconfig writes it (lower case and
  # compressed for IPv6), or ADDRESS as it is when it isn't one
  "$REPO_DIR/.venv/bin/python" -I -c '
import ipaddress, sys
try:
    print(ipaddress.ip_address(sys.argv[1]))
except ValueError:
    print(sys.argv[1])' "$1"
}

check_port() { # PORT as config.py takes it, a number from 1 to 65535, without leading zeros
  if ! [[ $PORT =~ ^[0-9]+$ ]] || (( 10#$PORT < 1 || 10#$PORT > 65535 )); then
    die "PORT must be a number from 1 to 65535, not $PORT"
  fi
  PORT=$((10#$PORT))
}

has_address() { # has_address ADDRESS: whether an interface has it, IPv4 or IPv6
  [[ $1 == 127.0.0.1 ]] && return 0
  ifconfig | awk -v address="$1" '($1 == "inet" || $1 == "inet6") && ($2 == address || index($2, address "%") == 1) {found = 1} END {exit !found}'
}

lan_address() { # the address of the interface the default route goes through
  local iface
  iface="$(route -n get default 2>/dev/null | awk '/interface:/ {print $2}')"
  [[ -n $iface ]] && ipconfig getifaddr "$iface" 2>/dev/null || true
}

find_herdr() { # the installed herdr, else the one on PATH, else one in the usual places
  local path
  for path in "$(installed HERDR_BIN)" "$(command -v herdr || true)" \
    "$HOME/.local/bin/herdr" /opt/homebrew/bin/herdr /usr/local/bin/herdr; do
    if [[ -n $path && -x $path ]]; then echo "$path"; return 0; fi
  done
  return 0
}

listener() { # listener PORT: the PID listening on PORT, "?" when only netstat sees one
  local pid
  pid="$(lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true)"
  if [[ -n $pid ]]; then echo "$pid"; return 0; fi
  # lsof sees only your own processes; netstat sees every listener, without its PID.
  netstat -anv -p tcp 2>/dev/null \
    | awk -v port="$1" '$6 == "LISTEN" && $4 ~ ("[.]" port "$") {found = 1} END {if (found) print "?"}' || true
}

command_of() { ps -o command= -p "$1" 2>/dev/null || true; }

ours() { # ours PID: whether PID is this service, the one launchd runs under its label
  # (the version being replaced has the same label). Not by command line: a bridge started
  # by hand can run the same command, and keeps the port.
  [[ $1 != "?" ]] || return 1
  [[ $1 == "$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk '$1 == "pid" {print $3; exit}' || true)" ]]
}

enrolled() { # enrolled KEY: the gateway's KEY (name, url) from the state file, or nothing
  [[ -f $STATE_FILE ]] && plutil -extract "gateway.$1" raw -o - "$STATE_FILE" 2>/dev/null || true
}

[[ $(uname -s) == Darwin ]] || die "this script is for macOS; on Linux see the systemd unit in agent-bridge/README.md"
[[ $EUID -ne 0 ]] || die "run as your own user, not with sudo: agent-bridge must run next to your herdr server"
[[ -f "$REPO_DIR/agent-bridge/pyproject.toml" ]] || die "run from a checkout of claude-remote-mcp"
command -v uv >/dev/null || die "uv not found; install it: https://docs.astral.sh/uv/getting-started/installation/"

log "Installing dependencies into $REPO_DIR/.venv"
(cd "$REPO_DIR" && uv sync --all-packages)
[[ -x $AGENT_BRIDGE ]] || die "uv sync didn't install $AGENT_BRIDGE"

# --- Settings: passed in, else installed, else asked ---------------------------------

[[ $SOURCE != plist ]] || log "Moving the settings from $PLIST to $ENV_FILE"

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

# Ask the gateway before trusting the state file: `check` saves a name the gateway has
# given the host since (`bridges rename`), and says whether it still has the host at all.
GATEWAY_GONE=""
if [[ $SOURCE == env_file && -n $(enrolled name) ]]; then
  GATEWAY_CHECK="$(bridge check 2>&1 | grep "the gateway at" || true)"
  case $GATEWAY_CHECK in
    # Only these two say it has no record of this host with this token (enrollment.py's
    # not_enrolled and unauthorized).
    *"isn't enrolled with the gateway"* | *"isn't this agent host's token"*)
      GATEWAY_GONE=1
      echo "warning: the gateway at $(enrolled url) didn't confirm it has this host as $(enrolled name); enrolling again fixes that" ;;
    # Anything else (no answer, or a new address it couldn't reach before the service
    # restarts) leaves the record as it is; the next start reports the address by itself.
    "FAIL  the gateway at"*)
      echo "warning: couldn't check with the gateway at $(enrolled url) right now (${GATEWAY_CHECK#*has this host }); keeping the enrollment as $(enrolled name)" ;;
  esac
fi

NAME_GIVEN=${NAME+x}
# Unless one is passed in, the name the gateway has now, e.g. after `bridges rename` there.
[[ -n $NAME_GIVEN || -z $(enrolled name) ]] || NAME="$(enrolled name)"
# The hostname as a valid name: a letter first, at most 32 characters.
DEFAULT_NAME="$(hostname -s | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"
[[ $DEFAULT_NAME =~ ^[a-z] ]] || DEFAULT_NAME="mac${DEFAULT_NAME:+-$DEFAULT_NAME}"
DEFAULT_NAME="$(printf '%s' "${DEFAULT_NAME:0:32}" | sed -E 's/-+$//')"
pick NAME NAME || ask NAME "This host's name in agent ids, like $DEFAULT_NAME/w1:p1" "$DEFAULT_NAME"
until bridge check --name-format "$NAME"; do
  ask NAME "This host's name" "" "required"
  [[ -n $NAME ]] || die "NAME is required"
done

# The installed address while this Mac still has it; otherwise its current one.
if [[ -z ${HOST+x} ]]; then
  INSTALLED_HOST="$(canonical "$(installed HOST)")"
  if [[ -n $INSTALLED_HOST ]] && has_address "$INSTALLED_HOST"; then
    HOST=$INSTALLED_HOST
  else
    HOST="$(lan_address)"
    if [[ -z $HOST ]]; then
      HOST=$INSTALLED_HOST # offline: kept, with the warning below
    elif [[ -n $INSTALLED_HOST ]]; then
      echo "The LAN address is now $HOST (was $INSTALLED_HOST)."
    fi
  fi
fi
HOST="$(canonical "$HOST")"
[[ -n $HOST ]] || die "couldn't find this Mac's LAN address; pass it, e.g. HOST=192.168.1.20"
# As config.py's check_host: an IP address, and not one that listens on every interface.
"$REPO_DIR/.venv/bin/python" -I -c '
import ipaddress, sys
sys.exit(ipaddress.ip_address(sys.argv[1]).is_unspecified)' "$HOST" 2>/dev/null \
  || die "HOST must be this Mac's IP address on the LAN, like 192.168.1.20, not $HOST"
has_address "$HOST" || echo "warning: no interface has $HOST right now; agent-bridge retries every 10 s until one does"

pick PORT PORT || PORT=8765
check_port
# A port in use is fine only when this service holds it, e.g. the version being replaced.
while PID="$(listener "$PORT")"; [[ -n $PID ]] && ! ours "$PID"; do
  if [[ $PID == "?" ]]; then
    echo "Port $PORT is in use by another user's program."
  else
    echo "Port $PORT is in use by PID $PID: $(command_of "$PID")"
  fi
  FREE=$((PORT + 1))
  while [[ -n $(listener "$FREE") ]]; do FREE=$((FREE + 1)); done
  ask PORT "Port" "$FREE"
  check_port
done

if [[ -z ${HERDR_BIN+x} ]]; then
  HERDR_BIN="$(find_herdr)"
  [[ -n $HERDR_BIN ]] || die "herdr not found; install it, or pass HERDR_BIN=/full/path/to/herdr"
fi
absolute HERDR_BIN
[[ -x $HERDR_BIN ]] || die "$HERDR_BIN is not an executable"

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
[[ -z ${AGENT_BRIDGE_TOKEN+x} ]] \
  || echo "warning: AGENT_BRIDGE_TOKEN is ignored: setup keeps the installed token, and ROTATE_TOKEN=1 makes a new one"
INSTALLED_TOKEN="$(installed TOKEN)"
TOKEN=$INSTALLED_TOKEN
NEW_TOKEN=""
if [[ -n ${ROTATE_TOKEN:-} || -z $TOKEN ]]; then
  TOKEN="$(openssl rand -hex 32)"
  NEW_TOKEN=1
fi
# What `agent-bridge check --token-format` checks, here so the token never shows in ps.
[[ ${#TOKEN} -ge 32 && $TOKEN =~ ^[A-Za-z0-9._~+/-]+=*$ ]] \
  || die "the installed token won't do; run again with ROTATE_TOKEN=1"

# Asked for until this host is enrolled, and again with a new token or a new name.
ENROLLED_AS="$(enrolled name)"
[[ -z $GATEWAY_GONE ]] || ENROLLED_AS=""
RENAMING=""
[[ -z $NAME_GIVEN || -z $ENROLLED_AS || $NAME == "$ENROLLED_AS" ]] || RENAMING=1
if [[ -z ${ENROLLMENT+x} ]] && [[ -z $ENROLLED_AS || -n $NEW_TOKEN || -n $RENAMING ]]; then
  printf '\nTo enroll with the gateway, run on the gateway host: sudo mcp-gateway enroll\n'
  [[ -z $RENAMING ]] || echo "The gateway has this host as $ENROLLED_AS; enrolling again renames it $NAME."
  ask ENROLLMENT "Enrollment string it printed, crm1@…" "" "Enter to enroll later"
fi
ENROLLMENT=${ENROLLMENT:-}
[[ -z ${ROTATE_TOKEN:-} || -n $ENROLLMENT ]] \
  || die "ROTATE_TOKEN needs an enrollment string, so the gateway learns the new token: run sudo mcp-gateway enroll on the gateway host and pass what it prints as ENROLLMENT"

# --- Install -------------------------------------------------------------------------

BS="\\"
Q="'"
env_line() { # env_line NAME VALUE: one env file line, single-quoted so nothing in it is
  # expanded; \ and ' escaped as python-dotenv reads them
  local value=${2//"$BS"/"$BS$BS"}
  value=${value//"$Q"/"$BS$Q"}
  printf "%s='%s'\n" "$1" "$value"
}

line() { env_line "AGENT_BRIDGE_$1" "$2"; }

plist_extras() { # the plist's other AGENT_BRIDGE_* settings, as env file lines
  local key
  for key in $(plist_value EnvironmentVariables | awk '$2 == "=" {print $1}'); do
    [[ $key == AGENT_BRIDGE_* && ! $key =~ ^AGENT_BRIDGE_($MANAGED)$ ]] || continue
    line "${key#AGENT_BRIDGE_}" "$(plist_value "EnvironmentVariables:$key")"
  done
}

write_env() {
  local key value
  log "Writing $ENV_FILE"
  mkdir -p "$SUPPORT_DIR"
  chmod 700 "$SUPPORT_DIR"
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
    # The other settings, by value: comments and hand-written quoting don't carry over.
    if [[ $SOURCE == env_file ]]; then
      while IFS= read -r -d '' key && IFS= read -r -d '' value; do
        env_line "$key" "$value"
      done < <(dotenv others "AGENT_BRIDGE_($MANAGED)")
    fi
    [[ $SOURCE != plist ]] || plist_extras
  } >"$TMP_ENV"
  mv "$TMP_ENV" "$ENV_FILE"
  SOURCE=env_file
}

write_plist() {
  log "Writing $PLIST"
  xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
  # A sed replacement, escaped for the | delimiter.
  fill() { xml "$1" | sed -e 's/[\\&|]/\\&/g'; }
  mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG_FILE")"
  TMP_PLIST="$(mktemp "$PLIST.XXXXXX")"
  sed -e "s|@AGENT_BRIDGE@|$(fill "$AGENT_BRIDGE")|" -e "s|@LOG_FILE@|$(fill "$LOG_FILE")|g" \
    "$DEPLOY_DIR/$LABEL.plist" >"$TMP_PLIST"
  chmod 644 "$TMP_PLIST"
  plutil -lint -s "$TMP_PLIST" || die "the generated plist is invalid"
  mv "$TMP_PLIST" "$PLIST"
}

start_service() {
  log "Starting $LABEL"
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl enable "$DOMAIN/$LABEL"
  # bootout returns before the old job is gone, so the first bootstrap can fail.
  local attempt
  for attempt in $(seq 10); do
    launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null && break
    [[ $attempt -lt 10 ]] || die "launchctl bootstrap failed; see: launchctl print $DOMAIN/$LABEL"
    sleep 1
  done
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
write_plist
start_service

# --- Enroll --------------------------------------------------------------------------

ENROLLED_NOW=""
GATEWAY_TOOK=""
NAME_TAKEN=""
while [[ -n $ENROLLMENT ]]; do
  log "Enrolling with the gateway"
  status=0
  output="$(bridge enroll "$ENROLLMENT" 2>&1)" || status=$?
  printf '%s\n' "$output"
  # It exits 1 too when the gateway took it but the state file couldn't be written.
  [[ $output != *"The gateway enrolled this host as"* ]] || GATEWAY_TOOK=1
  case $status in
    0) ENROLLED_NOW=1; break ;;
    3) # The gateway has another host by that name.
      NAME_TAKEN=$NAME
      NAME=""
      # It drops an old record only at the same address, e.g. one added with bridges add.
      echo "If $NAME_TAKEN is this Mac's old record (its address has changed), remove it on the gateway host with: sudo mcp-gateway bridges remove $NAME_TAKEN, then run again. Otherwise, choose another name."
      until [[ -n $NAME ]] && bridge check --name-format "$NAME"; do
        ask NAME "Another name for this host" "" "required"
        # Out of the enroll loop, past the token check below; the env file keeps the name.
        if [[ -z $NAME ]]; then NAME=$NAME_TAKEN; break 2; fi
      done
      NAME_TAKEN=""
      write_env
      start_service ;;
    4) break ;; # agent-bridge printed the command to run once the gateway is up
    *)
      # The gateway has it: another string wouldn't help with the state file.
      [[ -z $GATEWAY_TOOK ]] || break
      ask ENROLLMENT "Enrollment string, crm1@…" "" "Enter to enroll later" ;;
  esac
done

# Unless the gateway took the new token, it still has the installed one, and the new one
# would lock it out. `enroll` can fail after the gateway took it (a state file it can't
# write, or a lost answer), so its output is read, and the gateway asked when the state file
# names it. Without that, the installed token goes back on the chance the gateway has it.
if [[ -n ${ROTATE_TOKEN:-} && -z $ENROLLED_NOW && -z $GATEWAY_TOOK && -n $INSTALLED_TOKEN ]]; then
  if [[ $(bridge check 2>&1 || true) == *"ok    the gateway at"* ]]; then
    ENROLLED_NOW=1 # the gateway answers to the new token
  else
    TOKEN=$INSTALLED_TOKEN
    write_env
    start_service
    die "the gateway didn't confirm the new token, so the installed one is back; run again with ROTATE_TOKEN=1 and a new enrollment string"
  fi
fi
[[ -z $NAME_TAKEN ]] || die "the gateway has another host named $NAME_TAKEN; run again with another NAME"

URL_HOST=$HOST
[[ $HOST != *:* ]] || URL_HOST="[$HOST]"
cat <<EOF

Done. agent-bridge listens on http://$URL_HOST:$PORT and starts at every login.
If a check failed: tail $LOG_FILE (herdr failing means its server isn't running).
If the macOS firewall asks, allow incoming connections for Python.
${LOGIN_HINT:+
$LOGIN_HINT
}
EOF
GATEWAY_NAME="$(enrolled name)"
if [[ -n $GATEWAY_TOOK ]]; then
  echo "The gateway enrolled it, but its state file couldn't be written (see above). Fix that, then enroll again with a new string from sudo mcp-gateway enroll:"
  echo "  $AGENT_BRIDGE enroll '<what it printed>'"
elif [[ -n $GATEWAY_NAME && -z $ENROLLED_NOW && -n $NEW_TOKEN ]]; then
  echo "The gateway has it as $GATEWAY_NAME, but with an older token, so it can't reach it. Run sudo mcp-gateway enroll on the gateway host, then here:"
  echo "  $AGENT_BRIDGE enroll '<what it printed>'"
elif [[ -n $GATEWAY_NAME && -z $ENROLLED_NOW && -n $GATEWAY_GONE ]]; then
  echo "The gateway at $(enrolled url) didn't confirm it has it as $GATEWAY_NAME (see the warning above). Run sudo mcp-gateway enroll on the gateway host, then here:"
  echo "  $AGENT_BRIDGE enroll '<what it printed>'"
elif [[ -n $GATEWAY_NAME ]]; then
  echo "It is enrolled with the gateway at $(enrolled url) as $GATEWAY_NAME: Claude reaches its agents as $GATEWAY_NAME/…"
  [[ -z $RENAMING || -n $ENROLLED_NOW ]] \
    || echo "The gateway still has it as $GATEWAY_NAME, not $NAME: run again with NAME=$NAME and an enrollment string to rename it."
else
  echo "It is not enrolled with a gateway yet. Run sudo mcp-gateway enroll on the gateway host, then here:"
  echo "  $AGENT_BRIDGE enroll '<what it printed>'"
fi
