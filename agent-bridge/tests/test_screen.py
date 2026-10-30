import pytest

from agent_bridge.screen import last_lines, last_question

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


def test_drops_the_input_box_of_a_named_session() -> None:
    # A session renamed with /rename shows its name in the box's top rule; the box and the
    # status lines used to come back as output.
    snapshot = SNAPSHOT.replace(f"{RULE}\n❯", f"{RULE} grafik-loop ─\n❯")
    assert last_lines(snapshot, 3) == "  3\n  PONG\n✻ Sautéed for 2s"


WIDE = "─" * 200


def turn(*message: str, after: tuple[str, ...] = ()) -> str:
    """A screen 200 columns wide whose latest turn ends with `message`."""
    return "\n".join(
        ["❯ Fix the login bug.", "  Read 2 files", *message, "✻ Baked for 12s · done 6:46 AM",
         *after, "", WIDE, "❯", WIDE, "  ⏵⏵ auto mode on"]
    )


@pytest.mark.parametrize(
    ("message", "question"),
    [
        (["⏺ The fix is ready.", "  Do you want me to commit it?"], "Do you want me to commit it?"),
        (["⏺ Should it go on main or on the fix branch?"], "Should it go on main or on the fix branch?"),
        (["⏺ Two options:", "  - Is `main` right?"], "Is `main` right?"),
        # A question followed by context in the same paragraph still waits for an answer.
        (["⏺ Shall I run the installer? Otherwise you set it up yourself."],
         "Shall I run the installer? Otherwise you set it up yourself."),
        (["⏺ Fixed and pushed.", "  Nothing is left to do."], None),
        (["⏺ It's at https://example.com/page?id=3 now."], None),
        (["⏺ Do you want this?", "  Done: it's committed."], None),
    ],
)
def test_finds_the_question_a_turn_ended_with(message: list[str], question: str | None) -> None:
    assert last_question(turn(*message)) == question


def test_skips_recaps_and_notices_after_the_turn() -> None:
    snapshot = turn(
        "⏺ Shall I relaunch it?",
        after=("※ recap: We're fixing login. Next, relaunch?", "  ⎿  Read PLAN.md (70 lines)",
               "⏺ Compacted while idle, before the prompt cache expired"),
    )
    assert last_question(snapshot) == "Shall I relaunch it?"


def test_a_question_the_user_already_answered_is_not_reported() -> None:
    snapshot = turn("⏺ Do you want me to commit it?", after=("❯ yes", "  Ran 1 shell command"))
    assert last_question(snapshot) is None


def test_a_question_in_tool_output_is_not_the_agents() -> None:
    snapshot = turn("⏺ Bash(grep -r 'why?' .)", "  ⎿  docs/faq.md: why?")
    assert last_question(snapshot) is None


def test_a_long_question_is_cut_to_its_last_sentence() -> None:
    context = "The suite passes. " * 30
    snapshot = turn(f"⏺ {context}Should I push it? I'll wait.")
    assert last_question(snapshot) == "Should I push it?"


def test_no_question_without_claude_code_on_screen() -> None:
    assert last_question("$ ls\nwhy?\n") is None


def test_a_question_claude_code_wrapped_comes_back_whole() -> None:
    # Claude Code wraps long paragraphs itself, so they stay split even in herdr's
    # unwrapped text; a line reaching the screen's width (its rules') goes on below.
    wrapped = "  The service reads its key from the shared config, but the old one has its own App"
    rule = "─" * (len(wrapped) + 5)
    snapshot = "\n".join(
        ["❯ Design it.", "⏺ One decision is left.", wrapped,
         "  Configuration. Should the new client use the shared one?",
         "✻ Baked for 43s", rule, "❯", rule]
    )
    assert last_question(snapshot) == (
        "The service reads its key from the shared config, but the old one has its own App "
        "Configuration. Should the new client use the shared one?"
    )
    # A short line ends its paragraph.
    assert last_question(snapshot.replace(rule, "─" * 200)) == (
        "Configuration. Should the new client use the shared one?"
    )
