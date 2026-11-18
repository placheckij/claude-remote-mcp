"""`agent-bridge setup` and `uninstall` on Windows: the task, the firewall rule and finding the
running bridge. Never touches Task Scheduler, the firewall, real ports or a gateway."""

from pathlib import Path

import pytest

from agent_bridge.install import windows
from agent_bridge.install.common import Refused

SID = "S-1-5-21-1-2-3-1001"


def test_task_runs_the_windowless_bridge_with_its_log() -> None:
    xml = windows.task_xml(SID, r"C:\Users\Me & You\.local\bin\agent-bridgew.exe",
                           Path(r"C:\Users\Me & You\AppData\Local\agent-bridge\agent-bridge.log"),
                           Path(r"C:\Users\Me & You\AppData\Local\agent-bridge"))
    assert "{{" not in xml
    assert f"<UserId>{SID}</UserId>" in xml
    assert r"<Command>C:\Users\Me &amp; You\.local\bin\agent-bridgew.exe</Command>" in xml
    assert (r"<Arguments>serve --log-file &quot;C:\Users\Me &amp; You\AppData\Local\agent-bridge\agent-bridge.log"
            "&quot;</Arguments>") in xml
    task = windows.Task.parse(xml)
    assert task == windows.Task(r"C:\Users\Me & You\.local\bin\agent-bridgew.exe", True)


def test_task_refuses_a_path_task_scheduler_would_expand() -> None:
    with pytest.raises(Refused, match="%"):
        windows.task_xml(SID, r"C:\%USERNAME%\agent-bridgew.exe", Path(r"C:\log"), Path("C:\\"))


def test_a_disabled_task_reads_as_disabled_whatever_its_triggers_say() -> None:
    xml = windows.task_xml(SID, r"C:\b\agent-bridgew.exe", Path(r"C:\log"), Path("C:\\"))
    disabled = xml.replace("<Enabled>true</Enabled>\n    <Hidden>", "<Enabled>false</Enabled>\n    <Hidden>")
    assert disabled != xml
    assert windows.Task.parse(disabled).enabled is False
    # The triggers' Enabled stay true.
    assert disabled.count("<Enabled>true</Enabled>") == 2


def test_a_task_that_cannot_be_read_has_no_command() -> None:
    assert windows.Task.parse("not xml") == windows.Task("", True)


@pytest.mark.parametrize(("line", "known", "expected"), [
    (r'"C:\Users\me\.local\bin\agent-bridgew.exe" serve --log-file x', (), True),
    (r"C:\Python\pythonw.exe C:\Users\me\.local\bin\agent-bridgew.exe serve --log-file x", (), True),
    (r"C:\repo\.venv\Scripts\agent-bridge.exe serve", (), True),
    (r"C:\Python\python.exe -m agent_bridge.cli serve", (), True),
    (r"C:\repo\.venv\Scripts\uvicorn.exe agent_bridge.app:main --host 1.2.3.4", (), True),
    (r"C:\tools\bridge.exe serve", (r"C:\tools\bridge.exe",), True),
    (r"C:\tools\bridge.exe check", (r"C:\tools\bridge.exe",), False),
    (r"C:\Users\me\.local\bin\agent-bridge.exe check", (), False),
    (r"code.exe C:\repo\agent_bridge\serve.py", (), False),
    ("", (), False),
])
def test_is_bridge(line: str, known: tuple[str, ...], expected: bool) -> None:
    assert windows.is_bridge(line, known) is expected


def test_excluded_ranges_from_netsh() -> None:
    netsh = """
Protocol tcp Port Exclusion Ranges

Start Port    End Port
----------    --------
      5357        5357
     50000       50059     *

* - Administered port exclusions.
"""
    assert windows.excluded_ranges(netsh) == [(5357, 5357), (50000, 50059)]


def test_service_command_is_beside_the_shim_uv_ran() -> None:
    # uv's shim passes its own path without .exe.
    assert windows.service_command(r"C:\Users\me\.local\bin\agent-bridge") == r"C:\Users\me\.local\bin\agent-bridgew.exe"
    assert windows.service_command(r"C:\venv\Scripts\agent-bridge.exe") == r"C:\venv\Scripts\agent-bridgew.exe"


def test_quoted_for_powershell() -> None:
    assert windows.quoted("C:\\it's") == "'C:\\it''s'"
