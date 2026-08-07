//! Native Codex lifecycle notifications for the official QQ bot sandbox.

pub mod account_switch;
pub mod cli;
pub mod codex_accounts;
pub mod codex_login;
pub mod codex_usage;
pub mod commands;
pub mod daemon;
pub mod delivery;
pub mod formatting;
pub mod hooks;
pub mod installer;
pub mod locks;
pub mod logging_utils;
pub mod paths;
pub mod processes;
pub mod qq_client;
pub mod security;
pub mod store;
pub mod subprocess_utils;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
