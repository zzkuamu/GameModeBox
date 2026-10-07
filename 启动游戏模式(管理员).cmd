@echo off
rem GameModeBox launcher (Administrator) - ASCII only + CRLF on purpose
set "HERE=%~dp0"
if not exist "%HERE%main.pyw" (
  echo [ERROR] main.pyw not found next to this script
  pause
  exit /b 1
)
rem UAC elevation so the optional Nagle network tweak can apply.
rem If the project path contains spaces, edit the ArgumentList line below.
powershell -NoProfile -Command "Start-Process -FilePath 'C:\Windows\pyw.exe' -ArgumentList '%HERE%main.pyw' -Verb RunAs"
if errorlevel 1 (
  echo [ERROR] UAC prompt failed or was cancelled
  pause
  exit /b 1
)
exit /b 0
