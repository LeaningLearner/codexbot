//! Parsing and display of Codex app-server rate-limit snapshots.
//!
//! Codex has shipped two compatible, but differently nested, response
//! layouts.  This module intentionally accepts both and ignores unknown
//! metadata so newer app-server versions do not break the QQ command.

use chrono::{Local, TimeZone};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::HashSet;

pub const APP_SERVER_DASHBOARD_URL: &str = "https://chatgpt.com/codex/settings/usage";

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RateLimitBucket {
    pub name: String,
    pub used_percent: f64,
    pub window_duration_minutes: Option<f64>,
    pub resets_at: Option<f64>,
}

impl RateLimitBucket {
    pub fn remaining_percent(&self) -> f64 {
        (100.0 - self.used_percent).clamp(0.0, 100.0)
    }
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct RateLimitSnapshot {
    pub buckets: Vec<RateLimitBucket>,
}

fn as_object(value: &Value) -> Option<&Map<String, Value>> {
    value.as_object()
}

fn number(value: Option<&Value>) -> Option<f64> {
    let value = value?;
    let parsed = match value {
        Value::Number(value) => value.as_f64(),
        Value::String(value) => value.trim().parse::<f64>().ok(),
        _ => None,
    }?;
    parsed.is_finite().then_some(parsed)
}

fn bucket(value: &Value, name: String) -> Option<RateLimitBucket> {
    let raw = as_object(value)?;
    let used = number(raw.get("usedPercent"))?;
    let duration = number(raw.get("windowDurationMins"))
        .or_else(|| number(raw.get("windowDurationSeconds")).map(|seconds| seconds / 60.0));
    Some(RateLimitBucket {
        name,
        used_percent: used.clamp(0.0, 100.0),
        window_duration_minutes: duration,
        resets_at: number(raw.get("resetsAt")),
    })
}

fn value_text(value: Option<&Value>) -> Option<String> {
    match value? {
        Value::String(value) => {
            let value = value.trim();
            (!value.is_empty()).then(|| value.to_owned())
        }
        Value::Null => None,
        value => {
            let value = value.to_string();
            let value = value.trim();
            (!value.is_empty()).then(|| value.to_owned())
        }
    }
}

fn snapshot_base(raw: &Map<String, Value>, fallback: &str) -> String {
    value_text(raw.get("limitName"))
        .or_else(|| value_text(raw.get("limitId")))
        .unwrap_or_else(|| fallback.to_owned())
}

fn add_bucket(
    buckets: &mut Vec<RateLimitBucket>,
    seen: &mut HashSet<String>,
    name: &str,
    value: &Value,
) {
    let mut display_name = name.to_owned();
    let mut key = display_name.to_lowercase();
    let mut suffix = 2;
    while seen.contains(&key) {
        display_name = format!("{name}#{suffix}");
        key = display_name.to_lowercase();
        suffix += 1;
    }
    if let Some(parsed) = bucket(value, display_name) {
        seen.insert(key);
        buckets.push(parsed);
    }
}

fn snapshot_buckets(
    value: &Value,
    label: &str,
    buckets: &mut Vec<RateLimitBucket>,
    seen: &mut HashSet<String>,
) {
    let Some(raw) = as_object(value) else {
        return;
    };
    let base = snapshot_base(raw, "");
    let direct_name = if !label.is_empty() {
        label.to_owned()
    } else if !base.is_empty() {
        base.clone()
    } else {
        "limit".to_owned()
    };
    add_bucket(buckets, seen, &direct_name, value);

    for window_name in ["primary", "secondary"] {
        let Some(child) = raw.get(window_name).filter(|value| value.is_object()) else {
            continue;
        };
        let child_label = if !label.is_empty() || !base.is_empty() {
            format!("{direct_name}/{window_name}")
        } else {
            window_name.to_owned()
        };
        snapshot_buckets(child, &child_label, buckets, seen);
    }
}

pub fn parse_rate_limits(payload: &Value) -> RateLimitSnapshot {
    let Some(root) = payload.as_object() else {
        return RateLimitSnapshot::default();
    };
    let mut buckets = Vec::new();
    let mut seen = HashSet::new();

    let rate_limits = root
        .get("rateLimits")
        .and_then(Value::as_object)
        .filter(|raw| !raw.is_empty());
    if let Some(raw) = rate_limits {
        let label = snapshot_base(raw, "");
        if let Some(value) = root.get("rateLimits") {
            snapshot_buckets(value, &label, &mut buckets, &mut seen);
        }
    }

    let by_limit_id = root
        .get("rateLimitsByLimitId")
        .and_then(Value::as_object)
        .or_else(|| rate_limits.and_then(|raw| raw.get("rateLimitsByLimitId")?.as_object()));
    if let Some(by_limit_id) = by_limit_id {
        for (name, value) in by_limit_id {
            let label = value
                .as_object()
                .map(|raw| snapshot_base(raw, name))
                .unwrap_or_else(|| name.clone());
            snapshot_buckets(value, &label, &mut buckets, &mut seen);
        }
    }

    if buckets.is_empty() {
        for (name, value) in root {
            if matches!(name.as_str(), "rateLimits" | "rateLimitsByLimitId") {
                continue;
            }
            snapshot_buckets(value, name, &mut buckets, &mut seen);
        }
    }

    RateLimitSnapshot { buckets }
}

fn number_text(value: f64) -> String {
    if value.fract().abs() < f64::EPSILON {
        format!("{value:.0}")
    } else {
        format!("{value:.1}")
    }
}

fn window_text(minutes: Option<f64>) -> String {
    let Some(minutes) = minutes.filter(|value| value.is_finite() && *value > 0.0) else {
        return "未知".to_owned();
    };
    if minutes >= 24.0 * 60.0 && (minutes % (24.0 * 60.0)).abs() < f64::EPSILON {
        format!("{} 天", number_text(minutes / (24.0 * 60.0)))
    } else if minutes >= 60.0 && (minutes % 60.0).abs() < f64::EPSILON {
        format!("{} 小时", number_text(minutes / 60.0))
    } else {
        format!("{} 分钟", number_text(minutes))
    }
}

fn reset_text(timestamp: Option<f64>) -> String {
    let Some(timestamp) = timestamp.filter(|value| value.is_finite()) else {
        return "未知".to_owned();
    };
    let seconds = timestamp.trunc() as i64;
    let nanos = ((timestamp.fract().abs()) * 1_000_000_000.0) as u32;
    Local
        .timestamp_opt(seconds, nanos)
        .single()
        .map(|value| value.format("%Y-%m-%d %H:%M").to_string())
        .unwrap_or_else(|| "未知".to_owned())
}

pub fn format_usage_text(snapshot: &RateLimitSnapshot) -> String {
    format_usage_text_with_dashboard(snapshot, APP_SERVER_DASHBOARD_URL)
}

pub fn format_usage_text_with_dashboard(
    snapshot: &RateLimitSnapshot,
    dashboard_url: &str,
) -> String {
    if snapshot.buckets.is_empty() {
        return format!(
            "Codex 当前没有返回可用的限额 bucket；可能是旧版 Codex 或当前认证方式不支持读取用量。\n可查看用量面板：{dashboard_url}"
        );
    }

    let mut lines = vec!["Codex 用量（剩余）".to_owned()];
    for item in &snapshot.buckets {
        lines.push(format!(
            "{}：剩余 {}% · 窗口 {} · 重置 {}",
            item.name,
            number_text(item.remaining_percent()),
            window_text(item.window_duration_minutes),
            reset_text(item.resets_at),
        ));
    }
    lines.push(format!("用量面板：{dashboard_url}"));
    lines.join("\n")
}

pub fn usage_dashboard_hint() -> String {
    usage_dashboard_hint_with_url(APP_SERVER_DASHBOARD_URL)
}

pub fn usage_dashboard_hint_with_url(dashboard_url: &str) -> String {
    format!("可查看用量面板：{dashboard_url}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn keeps_primary_and_secondary_for_multiple_limit_ids() {
        let snapshot = parse_rate_limits(&json!({
            "rateLimitsByLimitId": {
                "workspace": {
                    "limitName": "Workspace",
                    "primary": {"usedPercent": 10},
                    "secondary": {"usedPercent": 20}
                },
                "personal": {
                    "limitId": "personal",
                    "primary": {"usedPercent": 30},
                    "secondary": {"usedPercent": 40}
                }
            }
        }));
        assert_eq!(
            snapshot
                .buckets
                .iter()
                .map(|bucket| bucket.name.as_str())
                .collect::<Vec<_>>(),
            [
                "Workspace/primary",
                "Workspace/secondary",
                "personal/primary",
                "personal/secondary"
            ]
        );
    }

    #[test]
    fn clamps_percentage_and_formats_remaining() {
        let snapshot = parse_rate_limits(&json!({
            "rateLimits": {"usedPercent": 125, "windowDurationMins": 300}
        }));
        assert_eq!(snapshot.buckets[0].remaining_percent(), 0.0);
        assert!(format_usage_text(&snapshot).contains("剩余 0%"));
    }
}
