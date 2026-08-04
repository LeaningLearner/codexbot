@echo off
setlocal

set "CODEXBOT_HOOK_ROOT=%~dp0"
if defined CODEXBOT_DATA_DIR (
  set "CODEXBOT_DATA=%CODEXBOT_DATA_DIR%"
) else (
  if not defined LOCALAPPDATA (
    echo {}
    exit /b 0
  )
  set "CODEXBOT_DATA=%LOCALAPPDATA%\CodexBot"
)

rem pythonw keeps the short-lived hook bootstrap from opening a console window.
set "CODEXBOT_RUNTIME=%CODEXBOT_DATA%\runtime\Scripts\pythonw.exe"
if not exist "%CODEXBOT_RUNTIME%" (
  echo {}
  exit /b 0
)

"%CODEXBOT_RUNTIME%" -E "%CODEXBOT_HOOK_ROOT%entry.py"
if errorlevel 1 echo {}
exit /b 0
