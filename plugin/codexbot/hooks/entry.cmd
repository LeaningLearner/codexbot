@echo off
setlocal

set "CODEXBOT_HOOK_ROOT=%~dp0"
if defined CODEXBOT_DATA_DIR (
  set "CODEXBOT_RUNTIME=%CODEXBOT_DATA_DIR%\runtime\Scripts\python.exe"
) else (
  if not defined LOCALAPPDATA (
    echo {}
    exit /b 0
  )
  set "CODEXBOT_RUNTIME=%LOCALAPPDATA%\CodexBot\runtime\Scripts\python.exe"
)

if not exist "%CODEXBOT_RUNTIME%" (
  echo {}
  exit /b 0
)

"%CODEXBOT_RUNTIME%" -E "%CODEXBOT_HOOK_ROOT%entry.py" 2>nul
if errorlevel 1 echo {}
exit /b 0
