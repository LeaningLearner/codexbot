@echo off
setlocal
chcp 65001 >nul

set "CODEXBOT_ROOT=%~dp0"
if not defined CODEXBOT_DATA_DIR goto :use_default_data
set "CODEXBOT_DATA=%CODEXBOT_DATA_DIR%"
goto :data_ready

:use_default_data
set "CODEXBOT_DATA=%LOCALAPPDATA%\CodexBot"

:data_ready
set "CODEXBOT_RUNTIME=%CODEXBOT_DATA%\runtime"
set "CODEXBOT_PYTHON=%CODEXBOT_RUNTIME%\Scripts\python.exe"
set "CODEXBOT_BASE_PYTHON="
set "CODEXBOT_BASE_PYTHON_ARGS="

py -3.11 -c "import sys; sys.exit(sys.version_info.major != 3 or sys.version_info.minor != 11)" >nul 2>&1
if errorlevel 1 goto :try_python_command
set "CODEXBOT_BASE_PYTHON=py"
set "CODEXBOT_BASE_PYTHON_ARGS=-3.11"
goto :python_ready

:try_python_command
python -c "import sys; sys.exit(sys.version_info.major != 3 or sys.version_info.minor != 11)" >nul 2>&1
if errorlevel 1 goto :python_missing
set "CODEXBOT_BASE_PYTHON=python"
set "CODEXBOT_BASE_PYTHON_ARGS="
goto :python_ready

:python_missing
echo [ERROR] 需要 Python 3.11，当前 py -3.11 和 python 命令均不符合要求。
goto :fail

:python_ready
if /I "%CODEXBOT_PARSE_ONLY%"=="1" goto :parse_only
if exist "%CODEXBOT_PYTHON%" goto :reuse_runtime

echo [1/4] 创建隔离 Python 运行时...
%CODEXBOT_BASE_PYTHON% %CODEXBOT_BASE_PYTHON_ARGS% -m venv "%CODEXBOT_RUNTIME%"
if errorlevel 1 goto :fail
goto :runtime_ready

:reuse_runtime
"%CODEXBOT_PYTHON%" -c "import sys; sys.exit(sys.version_info.major != 3 or sys.version_info.minor != 11)" >nul 2>&1
if errorlevel 1 goto :bad_runtime
echo [1/4] 复用现有隔离 Python 运行时。
goto :runtime_ready

:bad_runtime
echo [ERROR] Existing runtime is not Python 3.11.x. Recreate the runtime and run install.cmd again.
goto :fail

:runtime_ready

echo [2/4] 安装 CodexBot 与锁定依赖...
"%CODEXBOT_PYTHON%" -m pip install --disable-pip-version-check --only-binary=:all: --require-hashes --requirement "%CODEXBOT_ROOT%requirements.lock"
if errorlevel 1 goto :fail
"%CODEXBOT_PYTHON%" -m pip install --disable-pip-version-check --no-deps --no-build-isolation "%CODEXBOT_ROOT%."
if errorlevel 1 goto :fail

echo [3/4] 配置 QQ 凭据与个人 Codex 插件...
"%CODEXBOT_PYTHON%" -m codexbot.cli setup --repo-root "%CODEXBOT_ROOT%." %*
if errorlevel 1 goto :fail

echo [4/4] 安装完成。
echo 可运行 .\codexbot.cmd doctor 检查状态。
if not defined CODEXBOT_NO_PAUSE pause
exit /b 0

:parse_only
%CODEXBOT_BASE_PYTHON% %CODEXBOT_BASE_PYTHON_ARGS% -c "from pathlib import Path; import sys; root=Path(sys.argv[1]); root=root.resolve(); target=root / 'plugin' / 'codexbot'; ok=target.is_dir(); sys.exit(not ok)" "%CODEXBOT_ROOT%."
if errorlevel 1 goto :parse_failed
echo [OK] install.cmd 解析正常，已找到 Python 3.11，项目路径有效。
exit /b 0

:parse_failed
echo [ERROR] install.cmd 路径参数解析失败。
exit /b 1

:fail
echo.
echo [ERROR] 安装未完成，请保留上方错误信息并运行 .\codexbot.cmd doctor --offline 排查。
if not defined CODEXBOT_NO_PAUSE pause
exit /b 1
