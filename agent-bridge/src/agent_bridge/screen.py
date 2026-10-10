"""Turn a raw terminal snapshot into the lines worth sending to a caller.

Claude Code renders into a fixed-height screen: the conversation sits at the top,
then blank padding, then the input box (rule, `❯`, rule) and status lines. Taking the
last N raw lines would return mostly padding and chrome, so drop those first.
"""

import re

# Enough to cover a full screen plus some scrollback; trimmed down afterwards.
SNAPSHOT_LINES = 500

_RULE = re.compile(r"^\s*─{10,}\s*$")


def last_lines(snapshot: str, count: int) -> str:
    lines = [line.rstrip() for line in snapshot.splitlines() if line.strip()]
    for i in range(len(lines) - 1, 0, -1):
        if lines[i].strip() == "❯" and _RULE.match(lines[i - 1]):
            lines = lines[: i - 1]
            break
    return "\n".join(lines[-count:])
