@echo off
setlocal
if defined CODEXBOT_DATA_DIR (
  set "CODEXBOT_EXE=%CODEXBOT_DATA_DIR%\bin\codexbot.exe"
) else if defined LOCALAPPDATA (
  set "CODEXBOT_EXE=%LOCALAPPDATA%\CodexBot\bin\codexbot.exe"
) else (
  echo {}
  exit /b 0
)
if not exist "%CODEXBOT_EXE%" (
  echo {}
  exit /b 0
)
"%CODEXBOT_EXE%" hook 2>nul
if errorlevel 1 echo {}
exit /b 0
