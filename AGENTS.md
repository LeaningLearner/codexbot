# Repository Guidelines

## Project Structure & Module Organization

CodexBot is a Windows-focused Python 3.11 application. Core code lives in `src/codexbot/`: the CLI and daemon entry points coordinate hook ingestion, SQLite state, QQ delivery, credential handling, and process lifecycle. The installable Codex plugin is under `plugin/codexbot/`; keep its hook bootstrap small, standard-library-only, and unable to influence a Codex turn. Tests mirror modules in `tests/test_*.py`. Treat `build/`, `*.egg-info/`, `__pycache__/`, and local virtual environments as generated artifacts.

## Build, Test, and Development Commands

Run commands from the repository root in PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest tests\test_delivery.py -k rate
.\codexbot.cmd doctor --offline
```

The editable install provides the `codexbot` console entry point. The full test command runs the configured pytest suite; the focused example speeds up iteration. `doctor --offline` checks local installation state without contacting QQ. Before changing plugin metadata or hooks, also run the plugin validator command documented in `README.md`.

## Coding Style & Naming Conventions

Use four-space indentation, UTF-8, PEP 8 naming, and type annotations on public and asynchronous interfaces. Name modules, functions, and variables with `snake_case`, classes with `PascalCase`, and constants with `UPPER_SNAKE_CASE`. Preserve the existing `from __future__ import annotations` pattern. Keep platform-specific behavior explicit and isolate network, filesystem, and process side effects behind small functions. No formatter or linter is configured, so match surrounding import grouping and line length.

## Testing Guidelines

Tests use `pytest` and `pytest-asyncio` with automatic async mode. Name files `test_<module>.py` and cases `test_<behavior>`. Use `tmp_path` for state, `monkeypatch` for clocks or external calls, and recorder fakes for QQ delivery. Every bug fix should include a regression test; there is currently no enforced coverage threshold.

## Security & Configuration

Never commit QQ AppID/AppSecret values, access tokens, SQLite state, or logs. Credentials belong in Windows Credential Manager; runtime data belongs under `%LOCALAPPDATA%\CodexBot` (or `CODEXBOT_DATA_DIR` in tests). Ensure logs and exceptions redact secrets and message content where required.

## Commit & Pull Request Guidelines

No Git history is available in this checkout. Use concise, imperative commit subjects such as `Harden hook timeout handling`, and keep commits scoped. Pull requests should explain behavior and risk, list tests run, link relevant issues, and include screenshots or sample output for user-visible CLI or QQ message changes.
