"""Turn a raw terminal snapshot into the lines worth sending to a caller.

Claude Code renders into a fixed-height screen: the conversation sits at the top,
then blank padding, then the input box (rule, `❯`, rule) and status lines. Taking the
last N raw lines would return mostly padding and chrome, so drop those first.
"""

import re

# The most read at once: a full screen plus some scrollback; trimmed down afterwards.
SNAPSHOT_LINES = 500

# A named session (`/rename`) shows its name at the end of the box's top rule.
_RULE = re.compile(r"^\s*─{10,}(?: \S.* ─+)?\s*$")
# Longer questions are cut to their last sentence; they are read aloud.
MAX_QUESTION_CHARS = 300
# Claude Code wraps a paragraph at a word, so a wrapped line can end this far short of
# the screen's width.
WRAP_SLACK = 25


def last_lines(snapshot: str, count: int) -> str:
    return "\n".join(_content(snapshot)[-count:])


def _content(snapshot: str) -> list[str]:
    lines = [line.rstrip() for line in snapshot.splitlines() if line.strip()]
    # The input box is a rule followed by the `❯` line, which may hold draft text.
    for i in range(len(lines) - 1, 0, -1):
        if lines[i].lstrip().startswith("❯") and _RULE.match(lines[i - 1]):
            return lines[: i - 1]
    return lines


def last_question(snapshot: str) -> str | None:
    """The question an idle agent ended its turn with, if its final message's last
    paragraph asks one. Herdr calls such an agent idle (or done), like any other that
    finished; this is what tells them apart. A guess from the screen, so a question put
    differently can be missed."""
    # The input box's rules span the screen; without them, nothing is taken as wrapped.
    rules = [len(line.strip()) for line in snapshot.splitlines() if _RULE.match(line)]
    width = max(rules, default=None)
    lines = _content(snapshot)
    # Only what came after the user's last prompt (`❯ …`) belongs to the latest turn.
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("❯"):
            lines = lines[i + 1 :]
            break
    # The turn ends with "✻ <verb> for 12s"; recaps and notices like "⏺ Compacted while
    # idle" can follow it.
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("✻"):
            lines = lines[:i]
            break
    starts = [i for i, line in enumerate(lines) if line.startswith("⏺")]
    if not starts:
        return None
    message = [lines[starts[-1]].removeprefix("⏺").strip()]
    previous = lines[starts[-1]]
    for line in lines[starts[-1] + 1 :]:
        if line.lstrip().startswith(("⎿", "※")):
            # Right after the ⏺ line, this is a tool call's output, not a message.
            if len(message) == 1:
                return None
            break
        # Claude Code wraps long paragraphs itself; a line that filled the screen's width
        # goes on in the next one.
        if width and len(previous) >= width - WRAP_SLACK:
            message[-1] += " " + line.strip()
        else:
            message.append(line.strip())
        previous = line
    paragraph = message[-1].removeprefix("- ").strip()
    # "?" ending a sentence, not one inside a URL.
    if not re.search(r"\?[\"')*`]*(\s|$)", paragraph):
        return None
    if len(paragraph) > MAX_QUESTION_CHARS:
        sentences = re.split(r"(?<=[.!?])\s+", paragraph)
        asked = [sentence for sentence in sentences if "?" in sentence]
        paragraph = asked[-1][-MAX_QUESTION_CHARS:]
    return paragraph
