"""Each README's table of contents lists its ## and ### headings, linked to the anchors
GitHub gives them, between <!-- toc --> and <!-- /toc -->. On a failure, paste the block
the message shows. Headings are written with #; an underlined one (=== or ---) is refused."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
READMES = ["README.md", "agent-bridge/README.md", "mcp-gateway/README.md"]
START, END = "<!-- toc -->", "<!-- /toc -->"
# Up to 3 spaces before, and closing #s after, as CommonMark allows.
HEADING = re.compile(r" {0,3}(#{1,6})\s+(.+?)(?:\s+#+)?\s*")
FENCE = re.compile(r"(`{3,}|~{3,})")
UNDERLINE = re.compile(r" {0,3}(=+|-+)\s*")
# Lines a --- under them doesn't make a heading of: a list item, a table row, a heading.
NOT_A_PARAGRAPH = re.compile(r"\s*([-*+]\s|\d+[.)]\s|\||#)")


def headings(text: str) -> list[tuple[int, str, str]]:
    """(level, title, anchor) of every heading outside code blocks, in order."""
    found = []
    used: dict[str, int] = {}
    fence = None
    previous = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        if fence is not None:
            # Closed only by the same character, at least as many, and nothing after them.
            if re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", stripped):
                fence = None
            previous = ""
            continue
        if opened := FENCE.match(stripped):
            fence = opened.group(1)
            previous = ""
            continue
        if UNDERLINE.fullmatch(line) and previous.strip() and not NOT_A_PARAGRAPH.match(previous):
            raise ValueError(f"{previous.strip()!r} is underlined into a heading; write it with # instead")
        previous = line
        if not (match := HEADING.fullmatch(line)):
            continue
        level, title = len(match.group(1)), match.group(2)
        # GitHub: lowercase, drop punctuation but "-" and "_", spaces to "-"; a repeat gets -1, -2, …
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        count = used.get(slug, 0)
        used[slug] = count + 1
        found.append((level, title, slug if count == 0 else f"{slug}-{count}"))
    return found


def toc(text: str) -> str:
    entries = [
        f"{'  ' * (level - 2)}- [{title}](#{anchor})"
        for level, title, anchor in headings(text)
        if level in (2, 3)
    ]
    return "\n".join([START, "**Contents**", "", *entries, END])


def test_anchors_are_githubs() -> None:
    text = "\n".join([
        "# Title",
        "## Optional: Cloudflare Access with a service token",
        "### When it's stopped or removed",
        "## As a service on macOS (launchd)",
        "```sh",
        "# a comment, not a heading",
        "```",
        "#### Restart, stop, uninstall",
        "#### Restart, stop, uninstall",
        "## Install with mcp-gateway setup",
    ])

    assert [anchor for _, _, anchor in headings(text)] == [
        "title",
        "optional-cloudflare-access-with-a-service-token",
        "when-its-stopped-or-removed",
        "as-a-service-on-macos-launchd",
        "restart-stop-uninstall",
        "restart-stop-uninstall-1",
        "install-with-mcp-gateway-setup",
    ]


def test_headings_as_commonmark_reads_them() -> None:
    text = "\n".join([
        "   ## Indented",
        "## Closed ##",
        "````md",
        "```sh",
        "## inside the outer block",
        "```",
        "````",
        "## After the block",
        "- an item",
        "---",
        "| a | b |",
        "|---|---|",
    ])

    assert [(level, title) for level, title, _ in headings(text)] == [
        (2, "Indented"), (2, "Closed"), (2, "After the block"),
    ]


@pytest.mark.parametrize("underline", ["===", "---"])
def test_an_underlined_heading_is_refused(underline: str) -> None:
    with pytest.raises(ValueError, match="write it with #"):
        headings(f"Intro\n\nA heading\n{underline}\n")


def test_the_toc_lists_sections_and_subsections_only() -> None:
    text = "# Title\n## One\n### One a\n#### One a i\n## Two\n"

    assert toc(text) == "\n".join([
        START, "**Contents**", "", "- [One](#one)", "  - [One a](#one-a)", "- [Two](#two)", END,
    ])


@pytest.mark.parametrize("readme", READMES)
def test_readme_toc_matches_its_headings(readme: str) -> None:
    text = (ROOT / readme).read_text()
    assert text.count(START) == 1 and text.count(END) == 1, f"{readme} needs one {START} … {END} block"
    current = text[text.index(START):text.index(END) + len(END)]
    expected = toc(text)

    assert current == expected, f"{readme}'s table of contents is out of date; replace it with:\n\n{expected}\n"
