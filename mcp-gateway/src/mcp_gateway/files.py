"""State files that the gateway and `mcp-gateway …` (cli.py) both change: each change holds
a lock file, and every file is written whole beside the old one and renamed over it."""

import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no lock, as for oauth.lock.
    fcntl = None

# Nothing here follows a link put in place of the lock or the file.
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@contextlib.contextmanager
def locked(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | NOFOLLOW, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock)


def read_bytes(path: Path) -> bytes:
    with os.fdopen(os.open(path, os.O_RDONLY | NOFOLLOW), "rb") as file:
        return file.read()


def write_json(path: Path, data: object) -> None:
    # As oauth.json: written beside it and renamed over it, so a crash never leaves half
    # a file, and a leftover temporary file is removed first so its old mode can't stay.
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
            json.dump(data, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
