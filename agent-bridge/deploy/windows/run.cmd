@echo off
rem What the agent-bridge scheduled task runs: agent-bridge serve, writing to its log.
rem setup.ps1 fills in the two paths and writes this to %LOCALAPPDATA%\agent-bridge.
rem (conhost.exe re-quotes the task's arguments, so nothing with quotes inside can go there.)

rem The paths below are UTF-8, as this file is.
chcp 65001 >nul
rem Off whatever the registry says, so a ! in a path stays as it is.
setlocal DisableDelayedExpansion
rem Each line in the log as it happens; and the env file, and the output, read as UTF-8.
set "PYTHONUNBUFFERED=1"
set "PYTHONUTF8=1"

rem The task starts this every minute when it isn't running, so the log is kept across
rem starts, and set aside as .1 only once it passes 1 MB. (The log's path written out, not
rem set as LOG, which agent-bridge would get, and a ${LOG} in the env file with it.)
for %%F in ("{{LOG}}") do if %%~zF GTR 1048576 move /y "{{LOG}}" "{{LOG}}.1" >nul
echo ==== %DATE% %TIME% starting agent-bridge >> "{{LOG}}"
"{{EXE}}" serve >> "{{LOG}}" 2>&1
