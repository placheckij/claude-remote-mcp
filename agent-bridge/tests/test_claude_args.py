from agent_bridge.claude_args import clean_args, session_id_in

UUID = "9f1eeae1-5463-4d93-9be4-9c5c75cf409f"


def test_keeps_flags_and_drops_session_flags():
    args = ["--permission-mode", "auto", "--remote-control", "webapp", "--resume", UUID, "-c", "--fork-session"]
    assert clean_args(args) == ["--permission-mode", "auto", "--remote-control", "webapp"]


def test_drops_positional_prompt():
    assert clean_args(["--model", "haiku", "fix the build"]) == ["--model", "haiku"]


def test_drops_everything_after_double_dash():
    assert clean_args(["--model", "haiku", "--", "--not-a-flag"]) == ["--model", "haiku"]


def test_optional_value_consumes_next_word():
    assert clean_args(["--remote-control", "fix X"]) == ["--remote-control", "fix X"]


def test_optional_value_without_value():
    assert clean_args(["--remote-control", "--model", "haiku"]) == ["--remote-control", "--model", "haiku"]


def test_variadic_consumes_until_next_flag():
    assert clean_args(["--add-dir", "/a", "/b", "--model", "haiku"]) == ["--add-dir", "/a", "/b", "--model", "haiku"]


def test_equals_form():
    assert clean_args(["--model=haiku", f"--session-id={UUID}"]) == ["--model=haiku"]


def test_unknown_flag_is_boolean_so_next_word_is_dropped():
    assert clean_args(["--brand-new-flag", "fix X"]) == ["--brand-new-flag"]


def test_print_mode_dropped():
    assert clean_args(["-p", "--model", "haiku"]) == ["--model", "haiku"]


def test_required_value_may_start_with_dash():
    assert clean_args(["--system-prompt", "-be brief", "fix X"]) == ["--system-prompt", "-be brief"]


def test_required_flag_at_end_has_no_value():
    assert clean_args(["--model"]) == ["--model"]


def test_session_id_from_each_form():
    assert session_id_in(["--session-id", UUID]) == UUID
    assert session_id_in([f"--resume={UUID}"]) == UUID
    assert session_id_in(["-r", UUID]) == UUID
    assert session_id_in(["--resume"]) is None
    assert session_id_in(["--resume", "my-session-title"]) is None  # not a uuid


def test_session_id_ignores_value_after_double_dash():
    assert session_id_in(["--", "--session-id", UUID]) is None
