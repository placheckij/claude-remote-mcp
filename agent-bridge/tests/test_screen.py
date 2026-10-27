from agent_bridge.screen import last_lines

RULE = "─" * 40
SNAPSHOT = "\n".join(
    [
        " ▐▛███▛█   Claude Code",
        "",
        "❯ Count to 3, then say PONG.",
        "",
        "⏺ 1",
        "  2   ",
        "  3",
        "  PONG",
        "",
        "✻ Sautéed for 2s",
        *[""] * 45,
        RULE,
        "❯",
        RULE,
        "  ⊙ personal │ scratchpad",
        "  ⏵⏵ auto mode on",
    ]
)


def test_drops_padding_input_box_and_status_lines() -> None:
    assert last_lines(SNAPSHOT, 3) == "  3\n  PONG\n✻ Sautéed for 2s"


def test_keeps_everything_when_count_exceeds_content() -> None:
    assert last_lines(SNAPSHOT, 100).splitlines()[0] == " ▐▛███▛█   Claude Code"


def test_leaves_non_claude_output_alone() -> None:
    assert last_lines("a\n\nb\n", 5) == "a\nb"


def test_drops_the_input_box_while_it_holds_draft_text() -> None:
    # Text typed into the box (by hand, or a prompt still being submitted) used to hide
    # the box, so small `lines` returned only the box and status lines.
    snapshot = SNAPSHOT.replace("\n❯\n", "\n❯ half-typed draft\n  that wraps\n")
    assert last_lines(snapshot, 3) == "  3\n  PONG\n✻ Sautéed for 2s"
