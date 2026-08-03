@echo off
setlocal
chcp 65001 >nul

set "CODEXBOT_ROOT=%~dp0"
set "CODEXBOT_DATA=%LOCALAPPDATA%\CodexBot"
set "CODEXBOT_RUNTIME=%CODEXBOT_DATA%\runtime"
set "CODEXBOT_PYTHON=%CODEXBOT_RUNTIME%\Scripts\python.exe"
set "CODEXBOT_BASE_PYTHON="

py -3.11 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "CODEXBOT_BASE_PYTHON=py -3.11"
if not defined CODEXBOT_BASE_PYTHON (
  python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1
  if not errorlevel 1 set "CODEXBOT_BASE_PYTHON=python"
)
if not defined CODEXBOT_BASE_PYTHON (
  echo [ERROR] 需要 Python 3.11，当前 python 命令不符合要求。
  goto :fail
)

if /I "%CODEXBOT_PARSE_ONLY%"=="1" (
  %CODEXBOT_BASE_PYTHON% -c "from pathlib import Path; import sys; root=Path(sys.argv[1]).resolve(); raise SystemExit(0 if (root/'plugin'/'codexbot').is_dir() else 1)" "%CODEXBOT_ROOT%."
  if errorlevel 1 (
    echo [ERROR] install.cmd 路径参数解析失败。
    exit /b 1
  )
  echo [OK] install.cmd 解析正常，已找到 Python 3.11，项目路径有效。
  exit /b 0
)

if not exist "%CODEXBOT_PYTHON%" (
  echo [1/4] 创建隔离 Python 运行时...
  %CODEXBOT_BASE_PYTHON% -m venv "%CODEXBOT_RUNTIME%"
  if errorlevel 1 goto :fail
) else (
  echo [1/4] 复用现有隔离 Python 运行时。
)

echo [2/4] 安装 CodexBot 与锁定依赖...
"%CODEXBOT_PYTHON%" -m pip install --disable-pip-version-check --requirement "%CODEXBOT_ROOT%requirements.lock"
if errorlevel 1 goto :fail
"%CODEXBOT_PYTHON%" -m pip install --disable-pip-version-check --no-deps "%CODEXBOT_ROOT%."
if errorlevel 1 goto :fail

echo [3/4] 配置 QQ 凭据与个人 Codex 插件...
"%CODEXBOT_PYTHON%" -m codexbot.cli setup --repo-root "%CODEXBOT_ROOT%." %*
if errorlevel 1 goto :fail

echo [4/4] 安装完成。
echo 可运行 .\codexbot.cmd doctor 检查状态。
if not defined CODEXBOT_NO_PAUSE pause
exit /b 0

:fail
echo.
echo [ERROR] 安装未完成，请保留上方错误信息并运行 .\codexbot.cmd doctor --offline 排查。
if not defined CODEXBOT_NO_PAUSE pause
exit /b 1
