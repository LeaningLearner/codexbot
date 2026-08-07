@echo off
setlocal
chcp 65001 >nul
if defined CODEXBOT_DATA_DIR (
  set "CODEXBOT_EXE=%CODEXBOT_DATA_DIR%\bin\codexbot.exe"
) else (
  set "CODEXBOT_EXE=%LOCALAPPDATA%\CodexBot\bin\codexbot.exe"
)
if not exist "%CODEXBOT_EXE%" (
  echo CodexBot 尚未安装，请先运行 install.cmd。
  exit /b 1
)
"%CODEXBOT_EXE%" %*
exit /b %errorlevel%
