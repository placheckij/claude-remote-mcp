import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_settings_from_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings reads AGENT_BRIDGE_* even when built directly; keep the caller's own out.
    for name in list(os.environ):
        if name.startswith("AGENT_BRIDGE_"):
            monkeypatch.delenv(name)


@pytest.fixture
def set_home(monkeypatch: pytest.MonkeyPatch) -> Callable[[Path], None]:
    """Point `Path.home()` at a folder: HOME on macOS and Linux, USERPROFILE on Windows."""

    def set_home(path: Path) -> None:
        monkeypatch.setenv("HOME", str(path))
        monkeypatch.setenv("USERPROFILE", str(path))

    return set_home


@pytest.fixture
def symlink() -> Callable[[Path, Path], None]:
    """Make `link` point at `target`. Windows allows symlinks only with Developer Mode or
    admin rights (CI has them); without, a folder gets a junction, which paths resolve
    through the same way, and a file skips the test."""

    def symlink(link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as error:
            if sys.platform != "win32" or error.winerror != 1314:
                raise
            if not target.is_dir():
                pytest.skip("symlinks to files need Developer Mode or admin rights on Windows")
            import _winapi

            _winapi.CreateJunction(str(target), str(link))

    return symlink
