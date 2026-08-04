<p align="center">
  <img src="docs/images/codexbot-cover.png" alt="CodexBot cover" width="100%">
</p>

<h1 align="center">CodexBot</h1>

<p align="center">
  把 Codex 的关键状态带到你的手机上<br>
  Bring the important moments of Codex to your phone
</p>

<p align="center">
  <a href="#中文">简体中文</a> · <a href="#english">English</a>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/release/python-3110/"><img src="https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white" alt="Python 3.11"></a>
  <a href="https://www.microsoft.com/windows"><img src="https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D4?logo=windows&logoColor=white" alt="Windows 10/11"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT License"></a>
</p>

## 中文

### 它解决了什么问题？

过去，使用 Codex 写程序意味着你要像监工一样频繁刷新屏幕，紧盯它的每一步操作。

CodexBot 彻底改变了这种体验：它化身为你的“远程助手”，在后台全程托管 Codex 的编程任务，只把那些必须由你拍板的必要事项，精准推送到 QQ 通知你。从此，你不再需要盯屏等待；只要在手机上收到消息时查看提醒，在需要决定时回到 Codex 完成确认，编程过程就能继续推进。

CodexBot 是一个运行在 Windows 本机的 Codex 生命周期通知桥接器。它读取 Codex Hooks，将任务开始、任务完成和可选的权限提醒放入本地队列，再通过 QQ 官方 Bot 沙箱发送给已绑定的 QQ 用户。

> QQ 端是只读通知入口，不能直接批准、拒绝或远程控制 Codex。真正需要确认的操作仍然在 Codex 中完成。

### 核心能力

- 任务开始通知：项目、模型、时间和脱敏后的提示词摘要。
- 任务完成通知：完整的 `last_assistant_message`，超过 QQ 限制时自动分段。
- 子智能体静默：只通知主任务的一次总开始和总结果，不推送子智能体的启动、提示词或结束结果。
- 权限提醒：默认关闭，显式开启后才通知人工确认请求；自动审查不会默认制造“等待人工审批”的噪音。
- 多窗口支持：多个 Codex 窗口和多个项目共享一个 daemon 与消息队列，会话按 `session_id + 工作目录` 隔离。
- 可靠投递：SQLite WAL、本地 outbox、速率限制、重试、分段和永久错误处理。
- Codex 账号与用量：优先复用 `codex_login` 的 `~/.codex-switcher/accounts.json`，直接读取当前 ChatGPT OAuth 账号的用量；没有账号库时回退到本机 app-server。
- 隐私保护：AppSecret 存放在 Windows Credential Manager；提示词预览、错误和日志中的常见密钥会脱敏。
- 账号安全：access token 只在读取现有 Codex 凭据和请求官方用量接口时短暂保存在内存中，不写入 CodexBot 数据库、日志或 QQ；切换账号时更新 Codex 官方 `auth.json`，不复制凭据到 CodexBot 数据目录。
- 只读设计：只读取官方账号/用量接口，不调用模型 API，不创建第二个 Codex/ChatGPT 会话，也不从 QQ 远程执行任意命令。

### 环境要求

- Windows 10 或 Windows 11（x64；当前哈希锁固定为 `win_amd64` wheel）。
- Python 3.11.x。项目要求 `>=3.11,<3.12`，安装器会优先查找 `py -3.11`。
- 已安装并能正常运行的 Codex Desktop 或 Codex CLI，且支持 Codex Plugins / Lifecycle Hooks。
- `/usage`、`/codex_account`、`/codex_accounts`、`/codex_switch` 和 `/codex_login` 已在 Codex CLI 0.146.0 验证；缺少 app-server auth endpoint 时，已有 `codex_login` 账号仍可通过官方 backend 用量接口读取。
- 一个 QQ 官方机器人沙箱应用，并取得 AppID、AppSecret；需要在 QQ 开放平台启用私聊事件和主动消息能力。
- 你的 QQ 账号已经加入该机器人沙箱。
- 安装依赖和运行通知时需要网络；不需要 OpenAI API Key。

### QQ 开放平台：创建机器人沙箱应用

QQ 开放平台的页面名称可能随版本更新而变化，下面按当前常见路径说明配置方法。

1. 打开 [QQ 开放平台](https://q.qq.com/)，进入“机器人”页面，选择“去创建或管理我的 QQ 机器人”。

   ![QQ 开放平台机器人入口](docs/images/qq/01-open-platform.png)

2. 创建机器人应用，并在机器人详情的“开发设置”中查看 AppID 接入凭证。AppID 可以写入本地受保护配置；AppSecret 只能写入 Windows Credential Manager 或其他受保护的环境变量。

   ![QQ 机器人开发设置](docs/images/qq/02-development-settings.png)

3. 在“事件与回调配置”中按控制台提供的方式启用事件接收；本项目使用 QQ 官方 BotPy 沙箱能力，并通过一次性配对码限制可读取通知的 QQ 用户。

4. 在沙箱成员设置中把接收通知的 QQ 号加入沙箱，并确认机器人可以对该 QQ 主动发送私聊消息。

不要把真实 AppSecret、配对码、Access Token、QQ 号或 SQLite 数据库提交到 Git；如果凭据曾经出现在截图、日志或公开仓库中，应立即在 QQ 开放平台重新生成。

### 安装（完整下载与安装流程）

#### 1. 准备环境

- Windows 10 或 Windows 11（x64）。
- Python 3.11.x。如果尚未安装，请到 [python.org](https://www.python.org/downloads/release/python-3110/) 下载 Python 3.11 安装包，安装时勾选“Add python.exe to PATH”（保留默认的 py launcher）。安装器会优先查找 `py -3.11`。
- Git（可选）：已安装 Git 时用下面的 `git clone` 下载；没有 Git 时可在 GitHub 仓库页面点击 Code → Download ZIP 下载并解压。
- 已按上文在 QQ 开放平台创建机器人沙箱应用，并取得 AppID、AppSecret。
- 你的 QQ 账号已经加入该机器人沙箱，且允许机器人主动发送私聊消息。

#### 2. 下载源码

在 PowerShell 或命令提示符中执行：

```bat
git clone https://github.com/LeaningLearner/codexbot.git
cd codexbot
```

使用 ZIP 下载时，进入解压后的 `codexbot` 目录即可。

#### 3. 运行安装器

```bat
.\install.cmd
```

安装器会依次完成：

1. `[1/4]` 在 `%LOCALAPPDATA%\CodexBot\runtime` 创建隔离的 Python 3.11 虚拟环境；已存在且版本正确时直接复用。
2. `[2/4]` 按 `requirements.lock` 安装锁定版本（带哈希校验）的依赖，再安装 CodexBot 本身。
3. `[3/4]` 配置 QQ 凭据并安装个人 Codex 插件：
   - 若 Windows Credential Manager 中还没有 QQ 凭据，会提示输入 AppID 和 AppSecret（AppSecret 输入时不回显）；
   - 已有凭据时继续使用；需要更换时执行 `.\install.cmd --replace-credentials`；
   - 将插件复制到个人插件目录，合并 `~/.agents/plugins/marketplace.json`，并执行 `codex plugin add`。
4. `[4/4]` 生成一次性 QQ 配对码，并提示下一步。

#### 4. 配对 QQ

安装完成后：

1. 重启 Codex。
2. 在 Codex 中打开 `/hooks`，检查并信任 `codexbot` 的六个生命周期 Hook（SessionStart、UserPromptSubmit、PermissionRequest、PostToolUse、Stop、SessionEnd）。
3. 打开任意 Codex 任务，使伴随进程启动。
4. 用沙箱 QQ 私聊机器人发送安装器显示的配对命令：

```text
/bind XXXX-XXXX
```

配对码默认 30 分钟有效；过期后重新生成：

```bat
.\codexbot.cmd pair
```

#### 5. 检查安装状态

```bat
.\codexbot.cmd doctor --offline
.\codexbot.cmd doctor
```

`--offline` 跳过 QQ 网络认证；不带参数时会额外检查 QQ 沙箱 Gateway 连接。

### 让 AI 帮你配置

配置过程可以交给 AI 助手完成：在 Codex 中直接说“帮我安装并配置 CodexBot”，或把本 README 发给其他 AI 助手（如 ChatGPT）按步骤执行。

1. **准备信息**：QQ 开放平台的 AppID，以及接收通知的 QQ 号。AppSecret 不需要提前告诉 AI。
2. **下载安装**：AI 依次执行上面的下载和安装命令；到提示输入 AppID 时，你在终端直接输入。AppSecret 也由你在终端输入（不回显，不会进入 AI 对话记录）。
3. **检查结果**：AI 运行 `.\codexbot.cmd doctor --offline` 并解读输出；发现问题会尝试修复或给出下一步。
4. **配对**：AI 提示你重启 Codex、在 `/hooks` 信任 codexbot Hooks，并把配对码显示给你；你在 QQ 私聊发送 `/bind XXXX-XXXX`。
5. **排查问题**：QQ 侧收不到通知时，把 `.\codexbot.cmd doctor` 的输出和 QQ 开放平台控制台截图发给 AI，它会对照上面的配置步骤逐项检查。

注意：不要把 AppSecret 粘贴到 AI 对话中。AI 只能做本机安装、检查和配对；QQ 开放平台上的账号、沙箱成员和凭据生成/重置仍需你在控制台完成。

### QQ 命令

| 命令 | 作用 |
| --- | --- |
| `/bind XXXX-XXXX` | 使用一次性配对码绑定 QQ 用户 |
| `/status` | 查看最近的 Codex 项目、模型和状态 |
| `/last [项目] [页码]` | 分页读取最近回复；不写项目时保持全局最近一次，单独写数字仍表示页码 |
| `/usage` | 查看所有限额 bucket 的剩余百分比、窗口和重置时间；不支持时给出用量面板链接 |
| `/codex_account` | 查看 Codex 邮箱、套餐和认证类型 |
| `/codex_accounts` | 列出 `codex_login` 保存的账号并标记当前账号 |
| `/codex_switch <序号\|名称\|ID>` | 切换 `codex_login` 账号；切换前需要关闭正在运行的 Codex |
| `/codex_login` | 启动设备码登录；返回 `verificationUrl` 和 `userCode`，完成后主动回报 |
| `/mute` | 暂停未来的主动通知，不补发静音期间的旧消息 |
| `/unmute` | 恢复未来的主动通知 |
| `/help` | 查看帮助 |

### 通知行为

- 提交任务时只保存脱敏后的提示词预览，最多 120 个字符。
- 停止事件会保存完整最终回复，并根据 QQ 消息限制自动分段发送。
- 子智能体生命周期在入队前过滤；主任务仍各保留一次开始和最终通知，权限请求不会被该过滤器吞掉。
- 权限通知默认关闭。如果确实需要人工确认提醒，请在启动 Codex 的终端中设置：

  ```powershell
  $env:CODEXBOT_NOTIFY_PERMISSION_REQUESTS = "1"
  ```

- 权限通知只是提醒，QQ 不能代替 Codex 完成批准或拒绝。

### 多窗口和多项目

多个 Codex 窗口可以同时运行不同项目：

- 默认都使用 `%LOCALAPPDATA%\CodexBot` 下的同一个 SQLite 数据库。
- `daemon.lock` 保证同一数据目录只运行一个 QQ daemon，避免同一机器人建立多个连接。
- 所有项目的事件进入同一个本地 outbox，但会话键包含工作目录，不会因为重复的 `session_id` 覆盖其他项目。
- QQ 绑定和静音状态仍是全局设置；`/last` 默认是所有项目的最近回复，也可以使用 `/last 项目名 [页码]` 选择项目。回复按 session 保留有限历史，并受默认 7 天隐私 TTL 约束。

如果多个项目使用同一 QQ 机器人，请不要为每个项目设置不同的 `CODEXBOT_DATA_DIR`。不同数据目录会绕过共享锁，可能启动多个 daemon。

### 本地数据和安全

运行数据默认位于 `%LOCALAPPDATA%\CodexBot`：

- `state.sqlite3`：会话状态、通知 outbox、配对和最近回复。
- `logs\`：诊断日志，避免记录完整提示词、完整回复和常见密钥。
- `runtime\`：CodexBot 专用 Python 环境。

请不要提交 AppSecret、Access Token、SQLite 数据库或日志。如果凭据曾经出现在公开仓库、截图或日志中，请立即在 QQ 开放平台重新生成。

为保证 `/last` 和最终通知确实是“完整回复”，CodexBot 会把最终回复原文保存在本机数据库并发送给已绑定的唯一 QQ 用户，不会改写其中看起来像 token 的代码。最终回复默认 7 天后清理；仍请避免让 Codex 在回复中输出真实密钥。

`/usage` 会优先读取 `~/.codex-switcher/accounts.json` 当前账号的 OAuth token，并请求 `https://chatgpt.com/backend-api/wham/usage`；未登录、API key、网络失败或旧版返回不支持时会提供官方用量面板：<https://chatgpt.com/codex/settings/usage>。这些查询不会启动模型推理，因此不会额外消耗 Codex 推理 token。`/codex_switch` 会把选中账号写入 `CODEX_HOME\auth.json` 并同步 `accounts.json`；为避免覆盖正在使用的登录状态，检测到 Codex/ChatGPT 进程时会拒绝切换。切换后请重启已打开的 Codex 窗口。

### 截图

![Codex Hooks 配置](docs/images/codex-hooks.png)

![QQ 通知示例](docs/images/qq-notification.png)

### 开发与验证

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe "%USERPROFILE%\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py" plugin\codexbot
```

### 相关文档

- [Codex Hooks](https://developers.openai.com/codex/hooks/)
- [Codex app-server](https://developers.openai.com/codex/app-server/)
- [Codex authentication](https://developers.openai.com/codex/auth/)
- [Codex CLI](https://developers.openai.com/codex/cli/)
- [Codex Plugins](https://developers.openai.com/plugins/build/plugins)
- [QQ BotPy](https://github.com/tencent-connect/botpy)
- [QQ 消息频控](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)

## English

### What problem does it solve?

Before CodexBot, using Codex often meant acting like a supervisor: refreshing the screen repeatedly and watching every step of an agentic coding task.

CodexBot turns that into a notification-first workflow. It lets Codex keep working locally in the background and sends only the moments that need your attention to QQ. You no longer need to wait in front of the screen; when your phone receives a notification, you can review it at a glance and return to Codex when a decision is required.

CodexBot is a Windows companion that observes Codex lifecycle hooks, stores events in a local SQLite outbox, and delivers task notifications through the official QQ Bot sandbox to one paired QQ user.

> QQ is a read-only notification channel. It cannot approve, reject, or remotely control Codex. Any real confirmation still happens inside Codex.

### Highlights

- Task-start notifications with the project, model, time, and a redacted prompt preview.
- Complete final replies from `last_assistant_message`, automatically split for QQ limits.
- Quiet subagents: only the root task's overall start and final result are sent; subagent starts, prompts, and finishes stay local.
- Optional permission reminders, disabled by default to keep automatic-review noise out of QQ.
- Multiple Codex windows and projects supported by one daemon and one local outbox, with sessions scoped by `session_id + working directory`.
- SQLite WAL, retries, rate limiting, adaptive message splitting, and permanent-error handling.
- Codex account and usage commands reuse `codex_login`'s `~/.codex-switcher/accounts.json` for direct ChatGPT backend usage reads, with the local app-server as a fallback.
- AppSecret stored in Windows Credential Manager; common secrets are redacted from previews, errors, and logs.
- Access tokens are held only in memory while reading the existing Codex account store and official usage endpoint. They are never written to CodexBot's SQLite database, logs, or QQ; `/codex_switch` updates the official `CODEX_HOME\auth.json` and the switcher's active-account marker.
- Full replies and queued notification payloads are retained locally for at most 7 days by default; `CODEXBOT_LAST_REPLY_TTL_SECONDS` and `CODEXBOT_OUTBOX_TTL_SECONDS` can override that window.
- Read-only by design: only official account/usage reads, no model API calls, no second Codex/ChatGPT session, and no arbitrary remote command execution from QQ.

### Requirements

- Windows 10 or Windows 11 on x64; the current hash lock pins `win_amd64` wheels.
- Python 3.11.x. The package requires `>=3.11,<3.12`.
- A working Codex Desktop or Codex CLI installation with Codex Plugins / Lifecycle Hooks support.
- `/usage`, `/codex_account`, `/codex_accounts`, `/codex_switch`, and `/codex_login` are verified with Codex CLI 0.146.0; the direct backend usage path still works when app-server auth endpoints are unavailable.
- An official QQ Bot sandbox application with an AppID and AppSecret, with private-message events and proactive messaging enabled.
- Your QQ account added to the bot sandbox.
- Network access for installation and QQ delivery. An OpenAI API key is not required.

### QQ Open Platform: create the bot application

QQ Open Platform page names may change between releases; the steps below follow the common paths at the time of writing.

1. Open [QQ Open Platform](https://q.qq.com/), go to the Bots page, and choose "create or manage my QQ bot".

   ![QQ Open Platform bot entry](docs/images/qq/01-open-platform.png)

2. Create a bot application and find the AppID credential under the bot's Development Settings. The AppID may be stored as a protected local configuration; the AppSecret must only be stored in Windows Credential Manager or another protected secret store.

   ![QQ bot development settings](docs/images/qq/02-development-settings.png)

3. Enable event receiving under Events & Callbacks as shown in the console. CodexBot uses the official QQ BotPy sandbox capability and limits notification readers to one QQ user bound with a one-time pairing code.

4. In the sandbox member settings, add the QQ number that should receive notifications and confirm the bot may send proactive private messages to it.

Never commit the real AppSecret, pairing codes, access tokens, QQ numbers, or SQLite databases. If a credential ever appears in a screenshot, log, or public repository, regenerate it in the QQ Open Platform immediately.

### Installation

#### 1. Prerequisites

- Windows 10 or Windows 11 on x64.
- Python 3.11.x. If not installed, download it from [python.org](https://www.python.org/downloads/release/python-3110/) and tick "Add python.exe to PATH" (keep the default py launcher). The installer looks for `py -3.11` first.
- Git (optional): with Git, use the `git clone` command below; without Git, download the repository ZIP (Code → Download ZIP) and extract it.
- A QQ bot sandbox application with AppID/AppSecret, created as described above.
- Your own QQ account added to the bot sandbox with proactive private messages allowed.

#### 2. Download the source

Run this from PowerShell or Command Prompt:

```bat
git clone https://github.com/LeaningLearner/codexbot.git
cd codexbot
```

When using the ZIP download, enter the extracted `codexbot` directory instead.

#### 3. Run the installer

```bat
.\install.cmd
```

The installer proceeds in four steps:

1. `[1/4]` Creates an isolated Python 3.11 virtual environment at `%LOCALAPPDATA%\CodexBot\runtime`; reuses it when it already exists with the right version.
2. `[2/4]` Installs pinned, hash-verified dependencies from `requirements.lock`, then CodexBot itself.
3. `[3/4]` Stores QQ credentials and installs the personal Codex plugin:
   - If no QQ credentials exist in Windows Credential Manager, it prompts for AppID and AppSecret (AppSecret input is not echoed);
   - Existing credentials are kept unless you pass `--replace-credentials`;
   - It copies the plugin to the personal plugin directory, merges `~/.agents/plugins/marketplace.json`, and runs `codex plugin add`.
4. `[4/4]` Generates a one-time QQ pairing code and prints the next steps.

To replace the QQ AppID/AppSecret later:

```bat
.\install.cmd --replace-credentials
```

#### 4. Pair with QQ

After installation:

1. Restart Codex.
2. Open `/hooks` in Codex and trust the six `codexbot` lifecycle hooks (SessionStart, UserPromptSubmit, PermissionRequest, PostToolUse, Stop, SessionEnd).
3. Open any Codex task so the companion process starts.
4. Send the pairing command shown by the installer to the bot via sandbox QQ:

```text
/bind XXXX-XXXX
```

Pairing codes are valid for 30 minutes by default. Regenerate one with:

```bat
.\codexbot.cmd pair
```

#### 5. Verify installation

```bat
.\codexbot.cmd doctor --offline
.\codexbot.cmd doctor
```

`--offline` skips the QQ network authentication; without it, the QQ sandbox Gateway is checked as well.

### Let an AI assistant configure CodexBot

You can delegate the setup to an AI assistant: say "install and configure CodexBot for me" inside Codex, or send this README to another assistant such as ChatGPT and follow its steps.

1. **Prepare the inputs**: your QQ Open Platform AppID and the QQ number that should receive notifications. The AppSecret does not need to be shared with the AI.
2. **Download and install**: the AI runs the download and installation commands above; when it prompts for the AppID, type it into the terminal yourself. Type the AppSecret there too (it is not echoed and never enters the AI conversation).
3. **Check the result**: the AI runs `.\codexbot.cmd doctor --offline` and interprets the output, fixing issues or explaining the next step.
4. **Pair**: the AI asks you to restart Codex and trust the codexbot hooks in `/hooks`, then shows you the pairing code; send `/bind XXXX-XXXX` to the bot in QQ.
5. **Troubleshoot**: if notifications never arrive, paste the `.\codexbot.cmd doctor` output and QQ Open Platform console screenshots back to the AI so it can check each configuration step.

Never paste the AppSecret into the AI conversation. The AI can only install, check, and pair locally; account management, sandbox members, and credential generation/reset on the QQ Open Platform remain yours to do in the console.

### Commands

| Command | Purpose |
| --- | --- |
| `/bind XXXX-XXXX` | Bind the QQ user with a one-time pairing code |
| `/status` | Show recent Codex projects, models, and states |
| `/last [project] [page]` | Read the latest reply page by page; omitting the project keeps the global-latest behavior, and a lone number remains a page number |
| `/usage` | Show every rate-limit bucket's remaining percentage, window, and reset time, with a dashboard fallback |
| `/codex_account` | Show the Codex email, plan, and authentication type |
| `/codex_accounts` | List accounts saved by `codex_login` and mark the active account |
| `/codex_switch <index\|name\|ID>` | Switch a saved `codex_login` account; Codex must be closed first |
| `/codex_login` | Start device-code login; returns `verificationUrl` and `userCode`, then reports completion proactively |
| `/mute` | Pause future proactive notifications without backfilling old ones |
| `/unmute` | Resume future proactive notifications |
| `/help` | Show the command help |

Subagent lifecycle events are filtered before they enter the outbox. The root task still gets one start and one final notification, while permission requests remain eligible for reminders.

To opt into manual permission reminders, set this before launching Codex:

```powershell
$env:CODEXBOT_NOTIFY_PERMISSION_REQUESTS = "1"
```

Permission reminders are informational only; QQ cannot approve the operation for you.

### Multiple windows and projects

Multiple Codex windows can run different projects at the same time. The default shared data directory is `%LOCALAPPDATA%\CodexBot`; `daemon.lock` keeps one QQ daemon per data directory, and the outbox stores events from all projects while scoping sessions by their working directory.

Binding and mute state are global to the paired bot. `/last` defaults to the newest reply across projects but accepts `/last project [page]`; replies are retained in bounded per-session history and expire under the default seven-day privacy TTL. If multiple projects use the same QQ bot, keep the default shared `CODEXBOT_DATA_DIR`; separate data directories can start separate daemons and cause duplicate QQ connections.

### Privacy and local data

Runtime data is stored under `%LOCALAPPDATA%\CodexBot`. QQ credentials stay in Windows Credential Manager. Prompt previews, CLI errors, and logs redact common secrets where possible. Do not commit credentials, access tokens, SQLite state, or logs. `/usage` prefers the existing `~/.codex-switcher/accounts.json` OAuth account and falls back to <https://chatgpt.com/codex/settings/usage> when direct usage is unavailable.

To keep `/last` and final notifications complete, CodexBot stores the final reply verbatim in the local database and sends it only to the single bound QQ user; it does not rewrite token-like source-code variables. Final replies expire after seven days by default, but you should still avoid asking Codex to print real secrets.

Account and rate-limit reads do not start model inference and therefore add no Codex inference-token usage. `/codex_switch` writes the selected account to `CODEX_HOME\auth.json` and updates the switcher's active-account marker; restart an already-open Codex window if it does not refresh immediately. Device-code login still keeps one local app-server child alive until the matching `account/login/completed` notification arrives, with cleanup on mismatch, timeout, cancellation, failure, or daemon shutdown.

### Related documentation

- [Codex app-server](https://developers.openai.com/codex/app-server/)
- [Codex authentication](https://developers.openai.com/codex/auth/)
- [Codex CLI](https://developers.openai.com/codex/cli/)

### Development

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe "%USERPROFILE%\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py" plugin\codexbot
```

## License

CodexBot is released under the [MIT License](LICENSE).
