# agent-bridge upgrade (#158)

Upgrade the installed agent-bridge package and re-run setup in one command, safely on
Windows.

## Problem

Upgrading on Windows takes three manual steps (disable and end the task, `uv tool upgrade`,
`agent-bridge setup`). Getting them wrong leaves the host without a bridge: a running
agent-bridge locks its files, so `uv tool upgrade` fails partway and removes the package.
The stop step breaks under Git Bash (path conversion of `/Change`), and an agent asked to
upgrade a host needs a broad permission rule for `schtasks /End`.

## Decisions

- **Run from outside the install.** The command an agent or person waits on can't be the
  `agent-bridge.exe` and tool environment being replaced. The documented command is

  ```
  uvx --from claude-remote-mcp-agent-bridge@latest agent-bridge upgrade
  ```

  uvx runs agent-bridge from its own environment in uv's cache, so the installed tool is
  not locked. It runs in the foreground, shows setup's output and returns a real exit code.
  The upgrade logic that runs is always the latest release's. No detached helper, no
  generated PowerShell, no log polling.
- On macOS and Linux a plain `agent-bridge upgrade` works too (files in use can be
  replaced). On Windows, `agent-bridge upgrade` run from the installed tool's own environment
  prints the uvx command and exits 1. A checkout's `.venv` or uvx's cache is not the installed
  tool, so those run the upgrade.
- **Latest only**: no version argument.
- **Editable, path, git or URL installs move to PyPI**, saying what they replace; the
  checkout itself is never touched.
- **If the upgrade and the reinstall both fail**, the old version is set up again when it is
  still there.
- Not included: a `--status` command, the bridge version in `agent-bridge check`.

## Steps

### 1. Find the install

- Windows, running from the installed tool's environment (`sys.prefix` resolves to
  `<uv tool dir>/claude-remote-mcp-agent-bridge`): print the uvx command, exit 1.
- Read `<uv tool dir>/claude-remote-mcp-agent-bridge/uv-receipt.toml` (`uv tool dir` gives
  the folder) and the version of the installed
  `claude_remote_mcp_agent_bridge-*.dist-info`. The receipt's requirement for
  `claude-remote-mcp-agent-bridge` decides the upgrade:
  - only a name (with or without a version specifier): **PyPI**, upgraded with
    `uv tool upgrade claude-remote-mcp-agent-bridge`. A pin is kept by uv, which itself prints
    that the tool is pinned and how to reinstall it at the latest version.
  - `editable`, `path`, `directory`, `git` or `url`: **moved to PyPI** with
    `uv tool install --reinstall claude-remote-mcp-agent-bridge`. Before it does that, upgrade
    prints the install it replaces (where it came from and its version).
- macOS with a Homebrew install (the paths `macos.py` already knows): **Homebrew**,
  upgraded with `brew upgrade agent-bridge`.
- Refused with the manual steps, exit 1: no install found, a pipx install, or two installs
  at once (macOS; the same detection setup refuses on).

### 2. Already current

Run from uvx's cache (`uvx …@latest`), the running copy's version is the latest release.
When the install is from PyPI and its version equals the running version, upgrade prints
`agent-bridge is up to date (0.3.1)`, stops nothing and exits 0. This shortcut is taken only
under uvx: a checkout's version says nothing about PyPI's.

Otherwise the version is compared before and after the package upgrade. When it didn't
change, the same message is printed and, on macOS and Linux, setup is skipped. On Windows
the bridge was stopped by then, so setup always runs. Homebrew: the same, before and after
`brew upgrade`.

### 3. Stop (Windows only)

- Print first: agent-bridge stops until setup starts it again; agents keep running in
  herdr, but the gateway can't reach them meanwhile (also an agent running this command
  from a herdr pane).
- Reuse `WindowsService._stop`: disable the task (writing the disabled marker), end it,
  stop agent-bridge processes serving, wait for the port to be free.
- Then make sure nothing still runs from the tool environment or the `~/.local/bin`
  `agent-bridge(w).exe` shims, e.g. an `agent-bridge check` in another terminal. If something
  does, put the task back (`WindowsService.abandon`) and refuse, naming the PIDs and
  programs, exit 1.
- The `_other_account` refusal applies as in setup.

On macOS and Linux nothing is stopped: the service keeps its open files through the upgrade
and setup restarts it.

### 4. Upgrade

Run the uv (or brew) command from step 1, output shown. When `uv tool upgrade` fails, fall
back to `uv tool install --reinstall claude-remote-mcp-agent-bridge`. Then print
`agent-bridge <old> -> <new>`. When an install moved from a checkout or wheel ends up on a
lower version, warn that it is a downgrade and print how to go back
(`uv tool install --editable <path>`, then `agent-bridge setup`).

### 5. Setup

Run the installed `agent-bridge setup` (the new one) as a child process with stdin, stdout
and stderr passed through, with no `AGENT_BRIDGE_*` in its environment, as setup's own
`bridge()` runs it. upgrade exits with setup's exit code. On Windows setup re-registers and
starts the task and removes the disabled marker. On a host that was never set up, setup asks
its questions as usual.

### 6. Both uv steps failed

- The old installed `agent-bridge --version` still runs: run that `agent-bridge setup` to
  bring the bridge back, then exit 1 saying the upgrade failed and the old version runs.
- It doesn't: print the recovery commands (`uv tool install --reinstall
  claude-remote-mcp-agent-bridge`, then `agent-bridge setup`) and exit 1. On Windows the
  disabled marker stays, so the next setup brings the task back.

## Structure

- `agent_bridge/install/upgrade.py`: the flow above, against a small `System` protocol
  (run a command with output shown, `uv tool dir`, read files, the installed version,
  run the installed agent-bridge, the platform, processes from the tool environment), so
  tests replace it. Windows stopping goes through `WindowsService`.
- `cli.py`: the `upgrade` subcommand, dispatched like `setup`.
- `README.md`: the uvx command in the Windows, macOS and Linux upgrade sections; the manual
  steps kept as a fallback; a note that agents can be allowed exactly that one command.

## Testing

`tests/test_upgrade.py` with a fake system:

- receipt parsing: PyPI, pinned, editable, path, git
- already up to date (uvx's version, and before/after on POSIX)
- Windows refusal when run from the installed tool's environment
- Windows stop, and a leftover process (task put back, refusal names it)
- `uv tool upgrade` failing and the reinstall succeeding
- both failing with the old exe still there, and with it gone
- moved from a checkout to a lower version: the downgrade warning
- Homebrew on macOS; pipx and two installs refused
- POSIX: nothing stopped, setup's exit code returned
