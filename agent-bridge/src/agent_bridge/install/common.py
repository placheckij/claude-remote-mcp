"""What every platform's setup shares: asking, and checking and writing the settings."""

import contextlib
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

from dotenv import dotenv_values
from pydantic import ValidationError

from agent_bridge.config import Settings, describe

# The next line typed, or None when there is no input left.
Read = Callable[[], str | None]

PREFIX = "AGENT_BRIDGE_"


class Refused(Exception):
    """Why setup stopped."""


def ask(read: Read, question: str, default: str = "", hint: str = "") -> str:
    """The answer, or `default` for Enter or no input left."""
    print(f"{question} [{hint or default or 'Enter to skip'}]: ", end="", file=sys.stderr, flush=True)
    answer = read()
    if answer is None:
        print(file=sys.stderr)
    # Windows PowerShell 5.1 starts what it pipes in with a byte order mark.
    return (answer or "").lstrip("﻿").strip() or default


def ask_optional(read: Read, question: str, default: str) -> str:
    """Like `ask`, but "-" answers none."""
    answer = ask(read, question, default, f"{default}; Enter for that, - for none" if default else "")
    return "" if answer == "-" else answer


def absolute(value: str, what: str) -> str:
    """`value` with a leading ~ expanded; refuses a relative path. Empty stays empty."""
    if not value:
        return value
    try:
        path = Path(value).expanduser()
    except RuntimeError:  # ~name with no such user
        raise Refused(f"{what} must be a full path, got {value}") from None
    if not path.is_absolute():
        raise Refused(f"{what} must be a full path, got {value}")
    return str(path)


def default_name(hostname: str, fallback: str) -> str:
    """The hostname as a valid host name: a letter first, at most 32 characters."""
    slug = re.sub(r"[^a-z0-9]+", "-", hostname.split(".")[0].lower()).strip("-")
    if not slug[:1].isalpha():
        slug = f"{fallback}-{slug}" if slug else fallback
    return slug[:32].rstrip("-")


def env_text(values: dict[str, str]) -> str:
    """The env file: one line per setting, single-quoted with \\ and ' escaped, as python-dotenv
    reads them. It expands ${...} even there, with no way to escape it, so that is refused."""
    lines = []
    for key, value in values.items():
        if "${" in value:
            raise Refused(f"{key} can't contain ${{: the bridge would read it as a variable")
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        lines.append(f"{key}='{escaped}'\n")
    return "".join(lines)


def check_settings(values: dict[str, str]) -> None:
    """Refuses settings the service would refuse to start with, or read differently."""
    text = env_text(values)
    with tempfile.TemporaryDirectory() as folder, _without_settings_in_env():
        path = Path(folder) / "env"
        path.write_text(text, encoding="utf-8")
        try:
            settings = Settings(_env_file=path)
            settings.check_combination()
        except ValidationError as error:
            raise Refused(describe(error, PREFIX).replace("Invalid settings:\n", "the bridge would refuse these settings:\n")) from None
        except RuntimeError as error:
            raise Refused(f"the bridge would refuse these settings: {error}") from None
        if settings.missing_for_service():
            raise Refused(f"the bridge would need {', '.join(settings.missing_for_service())}")
        if dotenv_values(path) != values:
            raise Refused("the env file wouldn't read back as written")


def write_file(path: Path, data: bytes, mode: int, folder_mode: int | None = None) -> None:
    """Written beside `path` with `mode` and renamed over it, so it is never half written or
    readable by others for a moment."""
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if folder_mode is not None:
            path.parent.chmod(folder_mode)
        temporary.unlink(missing_ok=True)
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as file:
            file.write(data)
        os.replace(temporary, path)
    except OSError as error:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise Refused(f"can't write {path} ({error.strerror})") from None


def warn_about_settings_in_env() -> None:
    for name in sorted(os.environ):
        if name.startswith(PREFIX):
            print(f"warning: {name} is ignored: setup writes the settings the service reads", file=sys.stderr)


@contextlib.contextmanager
def _without_settings_in_env() -> Iterator[None]:
    """Settings read AGENT_BRIDGE_* from the environment over the file; the service has none."""
    saved = {name: os.environ.pop(name) for name in list(os.environ) if name.startswith(PREFIX)}
    try:
        yield
    finally:
        os.environ.update(saved)
