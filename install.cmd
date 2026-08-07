@echo off
setlocal
chcp 65001 >nul

set "CODEXBOT_ROOT=%~dp0"
if defined CODEXBOT_DATA_DIR (
  set "CODEXBOT_DATA=%CODEXBOT_DATA_DIR%"
) else (
  set "CODEXBOT_DATA=%LOCALAPPDATA%\CodexBot"
)
set "CODEXBOT_BIN=%CODEXBOT_DATA%\bin"
set "CODEXBOT_EXE=%CODEXBOT_BIN%\codexbot.exe"
set "CODEXBOT_BUILD=%CODEXBOT_ROOT%target\release\codexbot.exe"

where cargo.exe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] 未找到 Rust Cargo。请先从 https://rustup.rs 安装稳定版 Rust 工具链。
  goto :fail
)

if /I "%CODEXBOT_PARSE_ONLY%"=="1" (
  if not exist "%CODEXBOT_ROOT%Cargo.toml" exit /b 1
  if not exist "%CODEXBOT_ROOT%plugin\codexbot\.codex-plugin\plugin.json" exit /b 1
  echo [OK] install.cmd 解析正常，已找到 Cargo、Rust 项目与 Codex 插件。
  exit /b 0
)

echo [1/4] 构建 CodexBot 原生发布版...
pushd "%CODEXBOT_ROOT%"
cargo build --release --locked
if errorlevel 1 (
  popd
  goto :fail
)
popd

echo [2/4] 安装 codexbot.exe...
if exist "%CODEXBOT_EXE%" "%CODEXBOT_EXE%" stop >nul 2>&1
if not exist "%CODEXBOT_BIN%" mkdir "%CODEXBOT_BIN%"
if errorlevel 1 goto :fail
copy /Y "%CODEXBOT_BUILD%" "%CODEXBOT_EXE%" >nul
if errorlevel 1 goto :fail

echo [3/4] 配置 QQ 凭据与个人 Codex 插件...
"%CODEXBOT_EXE%" setup --repo-root "%CODEXBOT_ROOT%." %*
if errorlevel 1 goto :fail

echo [4/4] 安装完成。
echo 可运行 .\codexbot.cmd doctor --offline 检查状态。
if not defined CODEXBOT_NO_PAUSE pause
exit /b 0

:fail
echo.
echo [ERROR] 安装未完成，请保留上方错误信息并运行 .\codexbot.cmd doctor --offline 排查。
if not defined CODEXBOT_NO_PAUSE pause
exit /b 1
