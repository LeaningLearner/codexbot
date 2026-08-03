@echo off
setlocal
chcp 65001 >nul
set "CODEXBOT_PYTHON=%LOCALAPPDATA%\CodexBot\runtime\Scripts\python.exe"
if not exist "%CODEXBOT_PYTHON%" (
  echo CodexBot 尚未安装，请先运行 install.cmd。
  exit /b 1
)
"%CODEXBOT_PYTHON%" -m codexbot.cli %*
exit /b %errorlevel%
