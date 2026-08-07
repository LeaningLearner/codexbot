# Repository Guidelines

## Project Structure & Module Organization

CodexBot is a Windows-focused Rust application. Runtime code lives in `src/`: the CLI and daemon coordinate hook ingestion, SQLite state, QQ delivery, credential handling, account switching, and process lifecycle. The installable Codex plugin is under `plugin/codexbot/`; keep its batch bootstrap tiny, network-free, and unable to influence a Codex turn. Unit tests live beside their modules and integration tests belong in `tests/`. Treat `target/`, local databases, logs, and editor state as generated artifacts.

## Build, Test, and Development Commands

Run commands from the repository root in PowerShell:

```powershell
cargo build
cargo fmt --all --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --all-targets --locked
cargo run -- doctor --offline
```

`cargo build --release --locked` is the production build used by `install.cmd`. `doctor --offline` checks local installation state without contacting QQ. Plugin structure and hook safety are covered by installer unit tests; before changing plugin metadata or hooks, also run the official plugin validator command documented in `README.md`.

## Coding Style & Naming Conventions

Use rustfmt defaults and idiomatic Rust naming: modules, functions, and variables use `snake_case`; types and traits use `PascalCase`; constants use `UPPER_SNAKE_CASE`. Public interfaces should have explicit error types or `anyhow::Result` at application boundaries. Keep Windows-specific code behind `cfg(windows)` and provide testable non-Windows behavior where practical. Isolate network, filesystem, credential, and process side effects behind small functions.

## Testing Guidelines

Every bug fix should include a regression test. Prefer deterministic unit tests for parsing and formatting, `tempfile` for state, and recorder fakes for QQ delivery. Tests must not contact QQ, modify real credentials, or inspect a user's live Codex account unless explicitly marked and opted into. There is no enforced coverage threshold, but the full `cargo test` suite must pass before committing.

## Security & Configuration

Never commit QQ AppID/AppSecret values, access tokens, SQLite state, account snapshots, or logs. QQ credentials belong in Windows Credential Manager; runtime data belongs under `%LOCALAPPDATA%\CodexBot` (or `CODEXBOT_DATA_DIR` in tests). Ensure logs and exceptions redact secrets and message content where required. Account snapshots must remain DPAPI-protected on Windows, and atomic writes must preserve the previous login on failure.

## Commit & Pull Request Guidelines

Use concise, imperative commit subjects such as `Rewrite CodexBot runtime in Rust`, and keep commits scoped. Pull requests should explain behavior and risk, list tests run, link relevant issues, and include screenshots or sample output for user-visible CLI or QQ message changes.
