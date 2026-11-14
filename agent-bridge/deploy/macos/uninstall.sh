#!/usr/bin/env bash
# Removes what setup.sh installed: leaves the gateway, then removes the launchd agent, its
# settings, its state and its log. The checkout and its .venv stay.
#   agent-bridge/deploy/macos/uninstall.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
AGENT_BRIDGE="$REPO_DIR/.venv/bin/agent-bridge"
LABEL=dev.claude-remote.agent-bridge
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_FILE="$HOME/Library/Logs/agent-bridge.log"
SUPPORT_DIR="$HOME/Library/Application Support/agent-bridge"
DOMAIN="gui/$(id -u)"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ $(uname -s) == Darwin ]] || die "this script is for macOS"
[[ $EUID -ne 0 ]] || die "run as your own user, not with sudo"

# First, while the token is still in the env file. It prints the gateway's command when
# the gateway can't be reached.
log "Leaving the gateway"
# Read before the state file goes, for the command to run by hand.
NAME="$(plutil -extract gateway.name raw -o - "$SUPPORT_DIR/state.json" 2>/dev/null || true)"
BY_HAND="if this host was enrolled, remove it on the gateway host with: sudo mcp-gateway bridges remove ${NAME:-<name>}"
if [[ -x $AGENT_BRIDGE ]]; then
  (
    for var in $(compgen -v AGENT_BRIDGE_); do unset "$var"; done
    exec "$AGENT_BRIDGE" unenroll
  ) || echo "warning: agent-bridge unenroll failed (above); $BY_HAND"
else
  echo "warning: $AGENT_BRIDGE is missing; $BY_HAND"
fi

log "Removing $LABEL"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
rm -f "$PLIST" "$LOG_FILE"
rm -f "$SUPPORT_DIR/env" "$SUPPORT_DIR/state.json" "$SUPPORT_DIR/state.json.tmp"
rmdir "$SUPPORT_DIR" 2>/dev/null || echo "Left $SUPPORT_DIR: it holds other files."

cat <<'EOF'

Done. agent-bridge is removed. Folder-trust entries it added to the agents' Claude config
stay; they're ordinary trust entries.
EOF
