@echo off
rem GameModeBox launcher - ASCII only + CRLF on purpose (cmd.exe parses .cmd as ANSI)
set "HERE=%~dp0"
if not exist "%HERE%main.pyw" (
  echo [ERROR] main.pyw not found next to this script
  pause
  exit /b 1
)
rem Resolve Python via the .pyw file association (pyw.exe -> the Python that
rem has PySide6). Do NOT use "where pythonw" here: a PySide6-less pythonw.exe
rem can sit earlier in PATH and the tool would die at import.
start "" "%HERE%main.pyw"
exit /b 0
