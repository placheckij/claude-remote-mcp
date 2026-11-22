"""Clean the arguments a Claude Code agent was started with so they can be restored.

Parsed the way Claude Code (commander.js) does, using the value tables from
`claude --help` (Claude Code 2.1). A flag missing from the tables is kept only when another
flag or nothing follows it: a word after it may be its value or a prompt, and a prompt must
never come back, so both go.
"""

import uuid
from collections.abc import Iterator, Sequence

REQUIRED = frozenset({
    "--agent", "--agents", "--append-system-prompt", "--autocompact", "--debug-file", "--effort",
    "--environment", "--fallback-model", "--input-format", "--json-schema", "--max-budget-usd",
    "--model", "-n", "--name", "--output-format", "--permission-mode", "--permission-prompts",
    "--plugin-dir", "--plugin-url", "--remote-control-session-name-prefix", "--session-id",
    "--setting-sources", "--settings", "--system-prompt", "--system-prompt-snapshot",
})
OPTIONAL = frozenset({
    "--cloud", "-d", "--debug", "--from-pr", "--prompt-suggestions", "--remote-control", "-r",
    "--resume", "--teleport", "-w", "--worktree",
})
VARIADIC = frozenset({
    "--add-dir", "--allowedTools", "--allowed-tools", "--betas", "--disallowedTools",
    "--disallowed-tools", "--file", "--mcp-config", "--tools",
})
# Flags that pick a session or a command-line prompt: a restore must not carry them.
DROPPED = frozenset({
    "-r", "--resume", "-c", "--continue", "--session-id", "--fork-session", "-p", "--print",
    "--from-pr", "--teleport", "--cloud", "-w", "--worktree",
})
SESSION_FLAGS = frozenset({"--session-id", "--resume", "-r"})
PICKS_SESSION = SESSION_FLAGS | {"--continue", "-c"}


def _flags(args: Sequence[str]) -> Iterator[tuple[str, list[str], str | None]]:
    """Yield (flag name, its tokens including values, its first value) for each flag.

    Positional tokens and everything after `--` are skipped.
    """
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--":
            return
        if not token.startswith("-") or token == "-":
            i += 1
            continue
        if token.startswith("--") and "=" in token:
            name, value = token.split("=", 1)
            yield name, [token], value
            i += 1
            continue
        end = i + 1
        if token in REQUIRED:
            end = min(end + 1, len(args))
        elif token in OPTIONAL:
            if end < len(args) and not args[end].startswith("-"):
                end += 1
        elif token in VARIADIC:
            while end < len(args) and not args[end].startswith("-"):
                end += 1
        elif end < len(args) and not args[end].startswith("-"):
            # Unknown: the word is its value or a prompt; clean_args drops both.
            end += 1
        yield token, list(args[i:end]), args[i + 1] if end > i + 1 else None
        i = end


def clean_args(args: Sequence[str]) -> list[str]:
    """The arguments after `claude` without session flags, print mode or a positional prompt."""
    return [t for name, tokens, _ in _flags(args) if _kept(name, tokens) for t in tokens]


def _kept(name: str, tokens: list[str]) -> bool:
    if name in DROPPED:
        return False
    return name in REQUIRED or name in OPTIONAL or name in VARIADIC or len(tokens) == 1


def picks_session(args: Sequence[str]) -> bool:
    """Whether these arguments already choose the conversation (--continue, --resume or
    --session-id, in any form)."""
    return any(name in PICKS_SESSION for name, _, _ in _flags(args))


def session_id_in(args: Sequence[str]) -> str | None:
    """The uuid given to --session-id, --resume or -r, or None (a title is not a uuid)."""
    for name, _, value in _flags(args):
        if name in SESSION_FLAGS and value is not None:
            try:
                return str(uuid.UUID(value))
            except ValueError:
                return None
    return None
