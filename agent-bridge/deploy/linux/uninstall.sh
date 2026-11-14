#!/usr/bin/env bash
# Removes what setup.sh installed: leaves the gateway, then removes the agent-bridge user
# service, its settings and its state. herdr's service is removed without stopping its
# server, so running agents stay until it stops. Lingering stays on. The checkout and its
# .venv stay.
#   agent-bridge/deploy/linux/uninstall.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
AGENT_BRIDGE="$REPO_DIR/.venv/bin/agent-bridge"
UNIT=agent-bridge.service
HERDR_UNIT=agent-bridge-herdr.service
UNIT_DIR="$HOME/.config/systemd/user"
CONFIG_DIR="$HOME/.config/agent-bridge"
STATE_DIR="$HOME/.local/state/agent-bridge"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ $(uname -s) == Linux ]] || die "this script is for Linux"
[[ $EUID -ne 0 ]] || die "run as your own user, not with sudo"
systemctl --user show-environment >/dev/null 2>&1 \
  || die "can't reach your systemd user manager; run this from your own login (SSH or a console), not through su or sudo -u"

# First, while the token is still in the env file. It prints the gateway's command when
# the gateway can't be reached.
log "Leaving the gateway"
if [[ -x $AGENT_BRIDGE ]]; then
  (
    for var in $(compgen -v AGENT_BRIDGE_); do unset "$var"; done
    exec "$AGENT_BRIDGE" unenroll
  )
else
  echo "warning: $AGENT_BRIDGE is missing; if this host was enrolled, remove it on the gateway host with: sudo mcp-gateway bridges remove <name>"
fi

log "Removing $UNIT"
systemctl --user disable --now --quiet "$UNIT" 2>/dev/null || true
rm -f "$UNIT_DIR/$UNIT"
HERDR_NOTE=""
if [[ -f $UNIT_DIR/$HERDR_UNIT ]]; then
  log "Removing $HERDR_UNIT"
  # Not stopped: that would stop every agent in herdr.
  systemctl --user disable --quiet "$HERDR_UNIT" 2>/dev/null || true
  rm -f "$UNIT_DIR/$HERDR_UNIT"
  ! systemctl --user is-active --quiet "$HERDR_UNIT" \
    || HERDR_NOTE="herdr's server keeps running with its agents until you stop it (herdr server stop) or reboot."
fi
systemctl --user daemon-reload
systemctl --user reset-failed "$UNIT" 2>/dev/null || true

log "Removing the settings and state"
rm -f "$CONFIG_DIR/env" "$STATE_DIR/state.json" "$STATE_DIR/state.json.tmp"
for dir in "$CONFIG_DIR" "$STATE_DIR"; do
  [[ ! -d $dir ]] || rmdir "$dir" 2>/dev/null || echo "Left $dir: it holds other files."
done

cat <<EOF

Done. agent-bridge is removed. Folder-trust entries it added to the agents' Claude config
stay; they're ordinary trust entries.${HERDR_NOTE:+
$HERDR_NOTE}
Lingering stays on, for any other user services. To turn it off:
  loginctl disable-linger $(id -un)
EOF
