#!/usr/bin/env bash
# Moved to agent-bridge/deploy/macos/setup.sh; this stub runs it, and goes in the next release.

set -euo pipefail

echo "note: setup-macos.sh is now agent-bridge/deploy/macos/setup.sh; running that" >&2
exec "$(dirname "${BASH_SOURCE[0]}")/macos/setup.sh" "$@"
