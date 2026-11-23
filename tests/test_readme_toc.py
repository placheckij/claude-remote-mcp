"""Each README's table of contents lists its ## and ### headings, linked to the anchors
GitHub gives them, between <!-- toc --> and <!-- /toc -->. On a failure, paste the block
the message shows."""

import re
from pathlib import Path

import pytest
from markdown_it import MarkdownIt

ROOT = Path(__file__).parent.parent
READMES = ["README.md", "agent-bridge/README.md", "mcp-gateway/README.md"]
START, END = "<!-- toc -->", "<!-- /toc -->"
# CommonMark, plus the GitHub tables these READMEs use.
MARKDOWN = MarkdownIt("commonmark").enable(["table", "strikethrough"])


def _label_and_text(inline) -> tuple[str, str]:
    """A heading as the TOC shows it (as written, but a link as its text: the entry is a
    link already) and as rendered text, which GitHub makes the anchor from."""
    label, text = [], []
    for child in inline.children or []:
        if child.type == "text":
            label.append(re.sub(r"([\\`*_~\[\]<>])", r"\\\1", child.content))
            text.append(child.content)
        elif child.type == "code_inline":
            # Its own run of backticks, spaced off if the code starts or ends with one.
            pad = " " if child.content.startswith("`") or child.content.endswith("`") else ""
            label.append(f"{child.markup}{pad}{child.content}{pad}{child.markup}")
            text.append(child.content)
        elif child.type in ("em_open", "em_close", "strong_open", "strong_close", "s_open", "s_close"):
            label.append(child.markup)
        elif child.type == "softbreak":
            label.append(" ")
            text.append(" ")
    return "".join(label).strip(), "".join(text).strip()


def headings(markdown: str) -> list[tuple[int, str, str]]:
    """(level, TOC label, anchor) of every heading, in order."""
    found = []
    # GitHub's slugger: a taken slug gets the next free -1, -2, …, counted per original.
    taken: dict[str, int] = {}
    tokens = MARKDOWN.parse(markdown)
    for opening, inline in zip(tokens, tokens[1:]):
        if opening.type != "heading_open":
            continue
        label, text = _label_and_text(inline)
        # Lowercase, punctuation and symbols dropped but "-" and "_", spaces to "-".
        original = slug = re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")
        while slug in taken:
            taken[original] += 1
            slug = f"{original}-{taken[original]}"
        taken[slug] = 0
        found.append((int(opening.tag[1]), label, slug))
    return found


def toc(markdown: str) -> str:
    entries = [
        f"{'  ' * (level - 2)}- [{label}](#{anchor})"
        for level, label, anchor in headings(markdown)
        if level in (2, 3)
    ]
    return "\n".join([START, "**Contents**", "", *entries, END])


def test_anchors_are_githubs() -> None:
    markdown = "\n".join([
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
        "## Step",
        "## Step 1",
        "## Step",
        "## [Linked](https://example.com) and `code`",
        "## __init__ files",
    ])

    assert [anchor for _, _, anchor in headings(markdown)] == [
        "title",
        "optional-cloudflare-access-with-a-service-token",
        "when-its-stopped-or-removed",
        "as-a-service-on-macos-launchd",
        "restart-stop-uninstall",
        "restart-stop-uninstall-1",
        "install-with-mcp-gateway-setup",
        "step",
        "step-1",
        "step-2",
        "linked-and-code",
        "init-files",
    ]


def test_headings_are_read_as_commonmark_reads_them() -> None:
    markdown = "\n".join([
        "   ## Indented",
        "## Closed ##",
        "Underlined",
        "----------",
        "````md",
        "```sh",
        "## inside the outer block",
        "```",
        "````",
        "```inline``` code starts this paragraph",
        "",
        "## After the blocks",
        "",
        "- an item",
        "  that goes on",
        "---",
        "> a quote",
        "---",
        "    ## indented four: code",
        "",
        "| a | b |",
        "|---|---|",
    ])

    assert [(level, label) for level, label, _ in headings(markdown)] == [
        (2, "Indented"), (2, "Closed"), (2, "Underlined"), (2, "After the blocks"),
    ]


def test_the_toc_shows_headings_as_written() -> None:
    markdown = "\n".join([
        "# Title",
        "## One `code`",
        "### One a",
        "#### One a i",
        "## [Linked](https://example.com)",
        "## Use `` a`b `` and ``` ``c ``` *quickly* __now__ ~~old~~",
    ])

    assert toc(markdown) == "\n".join([
        START, "**Contents**", "",
        "- [One `code`](#one-code)", "  - [One a](#one-a)", "- [Linked](#linked)",
        "- [Use ``a`b`` and ``` ``c ``` *quickly* __now__ ~~old~~](#use-ab-and-c-quickly-now-old)",
        END,
    ])


def test_a_toc_label_reads_back_as_its_heading() -> None:
    # Pasted into the README, the entry must render the heading's own text.
    heading = "Use `` a`b `` and ``` ``c ``` *quickly* __now__ ~~old~~ \\*literal\\*"
    (_, label, _), = headings(f"## {heading}\n")

    assert MARKDOWN.render(label) == MARKDOWN.render(heading)


@pytest.mark.parametrize("readme", READMES)
def test_readme_toc_matches_its_headings(readme: str) -> None:
    markdown = (ROOT / readme).read_text()
    assert markdown.count(START) == 1 and markdown.count(END) == 1, f"{readme} needs one {START} … {END} block"
    current = markdown[markdown.index(START):markdown.index(END) + len(END)]
    expected = toc(markdown)

    assert current == expected, f"{readme}'s table of contents is out of date; replace it with:\n\n{expected}\n"
