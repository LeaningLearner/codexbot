//! Reliable, rate-limited delivery of SQLite outbox items to QQ.

use chrono::{Local, TimeZone};
use rand::Rng;
use serde_json::Value;
use std::error::Error as StdError;
use std::future::Future;
use std::sync::Arc;
use std::time::{Duration, Instant};
use thiserror::Error;
use tokio::sync::Mutex;

use crate::formatting::{
    DEFAULT_CHUNK_SIZE, FormattingError, bisect_segment, render_segment, split_text,
};
use crate::security::redact_secrets;
use crate::store::{OutboxItem, Store, StoreError};

pub const LENGTH_MARKERS: &[&str] = &["40054007", "40054018", "长度超限", "消息过长"];
pub const DUPE_MARKERS: &[&str] = &["40054005", "消息被去重"];
pub const PERMANENT_MARKERS: &[&str] = &[
    "22006",
    "304061",
    "40034006",
    "40054013",
    "40034105",
    "40054004",
    "消息内容违规",
    "消息内容无效",
    "拒收",
    "无权限",
    "无好友关系",
];
pub const RATE_MARKERS: &[&str] = &[
    "40034100",
    "频控",
    "发送频率",
    "too many requests",
    "rate limit",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeliveryErrorCategory {
    Length,
    Duplicate,
    Permanent,
    Rate,
    Transient,
}

impl DeliveryErrorCategory {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Length => "length",
            Self::Duplicate => "duplicate",
            Self::Permanent => "permanent",
            Self::Rate => "rate",
            Self::Transient => "transient",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeliveryOutcome {
    Suppressed,
    Delivered,
    Split,
    Advanced,
    Retry,
    FailedPermanent,
}

impl DeliveryOutcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Suppressed => "suppressed",
            Self::Delivered => "delivered",
            Self::Split => "split",
            Self::Advanced => "advanced",
            Self::Retry => "retry",
            Self::FailedPermanent => "failed_permanent",
        }
    }
}

#[derive(Debug, Error)]
pub enum DeliveryError {
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error(transparent)]
    Formatting(#[from] FormattingError),
    #[error("unknown outbox kind: {0}")]
    UnknownKind(String),
}

#[derive(Debug, Clone)]
pub struct RateLimiter {
    minimum_interval: Duration,
    last_sent: Arc<Mutex<Option<Instant>>>,
}

impl RateLimiter {
    pub fn new(per_minute: u32) -> Result<Self, &'static str> {
        if per_minute == 0 {
            return Err("per_minute must be positive");
        }
        Ok(Self {
            minimum_interval: Duration::from_secs_f64(60.0 / f64::from(per_minute)),
            last_sent: Arc::new(Mutex::new(None)),
        })
    }

    pub fn per_minute(per_minute: u32) -> Self {
        Self::new(per_minute).expect("rate limit must be positive")
    }

    pub async fn wait(&self) {
        let mut last_sent = self.last_sent.lock().await;
        if let Some(previous) = *last_sent {
            let elapsed = previous.elapsed();
            if elapsed < self.minimum_interval {
                tokio::time::sleep(self.minimum_interval - elapsed).await;
            }
        }
        *last_sent = Some(Instant::now());
    }
}

impl Default for RateLimiter {
    fn default() -> Self {
        Self::per_minute(18)
    }
}

pub fn classify_delivery_error(error: &(dyn StdError + 'static)) -> DeliveryErrorCategory {
    let mut text = String::new();
    let mut current = Some(error);
    while let Some(error) = current {
        if !text.is_empty() {
            text.push(' ');
        }
        text.push_str(&error.to_string());
        current = error.source();
    }
    classify_delivery_error_text(&text)
}

pub fn classify_delivery_error_text(text: &str) -> DeliveryErrorCategory {
    let text = text.to_lowercase();
    if LENGTH_MARKERS
        .iter()
        .any(|marker| text.contains(&marker.to_lowercase()))
    {
        DeliveryErrorCategory::Length
    } else if DUPE_MARKERS
        .iter()
        .any(|marker| text.contains(&marker.to_lowercase()))
    {
        DeliveryErrorCategory::Duplicate
    } else if PERMANENT_MARKERS
        .iter()
        .any(|marker| text.contains(&marker.to_lowercase()))
    {
        DeliveryErrorCategory::Permanent
    } else if RATE_MARKERS
        .iter()
        .any(|marker| text.contains(&marker.to_lowercase()))
    {
        DeliveryErrorCategory::Rate
    } else {
        DeliveryErrorCategory::Transient
    }
}

fn payload_text(payload: &Value, key: &str, fallback: &str) -> String {
    match payload.get(key) {
        Some(Value::String(value)) if !value.is_empty() => value.clone(),
        Some(Value::Null) | None => fallback.to_owned(),
        Some(value) => value.to_string(),
    }
}

fn payload_number(payload: &Value, key: &str) -> Option<f64> {
    payload.get(key).and_then(|value| {
        value
            .as_f64()
            .or_else(|| value.as_str()?.parse::<f64>().ok())
    })
}

fn notification_stamp(item: &OutboxItem) -> String {
    let timestamp = payload_number(&item.payload, "created_at").unwrap_or(item.created_at);
    let seconds = timestamp.trunc() as i64;
    let nanos = (timestamp.fract().abs() * 1_000_000_000.0) as u32;
    Local
        .timestamp_opt(seconds, nanos)
        .single()
        .map(|value| value.format("%H:%M:%S").to_string())
        .unwrap_or_else(|| "00:00:00".to_owned())
}

pub fn notification_text(item: &OutboxItem) -> Result<String, DeliveryError> {
    let stamp = notification_stamp(item);
    let project = payload_text(&item.payload, "project", "unknown");
    match item.kind.as_str() {
        "task_started" => Ok(format!(
            "🚀 Codex 开始处理\n项目：{project}\n模型：{}\n时间：{stamp}\n任务：{}",
            payload_text(&item.payload, "model", "unknown"),
            payload_text(&item.payload, "preview", "（无文本预览）")
        )),
        "permission_required" => Ok(format!(
            "⏳ Codex 出现权限请求\n项目：{project}\n工具：{}\n原因：{}\n请在 Codex 中完成审批；如果已启用自动审批且任务继续，此消息可忽略。QQ 不能直接审批。",
            payload_text(&item.payload, "tool", "unknown"),
            payload_text(&item.payload, "reason", "需要确认的操作")
        )),
        "final_reply" => Ok(format!(
            "✅ Codex 已完成\n项目：{project}\n模型：{}\n\n{}",
            payload_text(&item.payload, "model", "unknown"),
            payload_text(&item.payload, "content", "")
        )),
        kind => Err(DeliveryError::UnknownKind(kind.to_owned())),
    }
}

pub async fn deliver_item<F, Fut, T, E>(
    store: &Store,
    item: &OutboxItem,
    openid: &str,
    mut sender: F,
    limiter: &RateLimiter,
) -> Result<DeliveryOutcome, DeliveryError>
where
    F: FnMut(&str, &str) -> Fut,
    Fut: Future<Output = Result<T, E>>,
    E: StdError + Send + Sync + 'static,
{
    if store.is_muted()? {
        store.mark_outbox(item.id, "suppressed", "notifications muted")?;
        return Ok(DeliveryOutcome::Suppressed);
    }

    let mut segments = match &item.segments {
        Some(segments) => segments.clone(),
        None => {
            let segments = split_text(&notification_text(item)?, DEFAULT_CHUNK_SIZE)?;
            store.prepare_segments(item.id, &segments)?;
            segments
        }
    };
    let index = if item.segments.is_some() {
        item.segment_index
    } else {
        0
    };
    if index >= segments.len() {
        store.mark_outbox(item.id, "delivered", "already complete")?;
        return Ok(DeliveryOutcome::Delivered);
    }

    let rendered = render_segment(&segments, index)?;
    limiter.wait().await;
    if let Err(error) = sender(openid, &rendered).await {
        let category = classify_delivery_error(&error);
        let safe_error = redact_secrets(&error.to_string());
        match category {
            DeliveryErrorCategory::Length => {
                if segments[index].chars().count() > 1 {
                    let (left, right) = bisect_segment(&segments[index])?;
                    segments.splice(index..=index, [left, right]);
                    store.replace_segments(item.id, &segments, index)?;
                    return Ok(DeliveryOutcome::Split);
                }
                store.mark_outbox(item.id, "failed_permanent", &safe_error)?;
                return Ok(DeliveryOutcome::FailedPermanent);
            }
            DeliveryErrorCategory::Duplicate => {
                store.advance_segment(item.id, index, segments.len())?;
                return Ok(if index + 1 >= segments.len() {
                    DeliveryOutcome::Delivered
                } else {
                    DeliveryOutcome::Advanced
                });
            }
            DeliveryErrorCategory::Permanent => {
                store.mark_outbox(item.id, "failed_permanent", &safe_error)?;
                return Ok(DeliveryOutcome::FailedPermanent);
            }
            DeliveryErrorCategory::Rate => {
                store.reschedule(item.id, 65.0, &safe_error)?;
                return Ok(DeliveryOutcome::Retry);
            }
            DeliveryErrorCategory::Transient => {
                let exponential = 5.0 * 2_f64.powi(item.attempts.min(6) as i32);
                let delay = exponential.min(300.0) + rand::thread_rng().gen_range(0.0..2.0);
                store.reschedule(item.id, delay, &safe_error)?;
                return Ok(DeliveryOutcome::Retry);
            }
        }
    }

    store.advance_segment(item.id, index, segments.len())?;
    Ok(if index + 1 >= segments.len() {
        DeliveryOutcome::Delivered
    } else {
        DeliveryOutcome::Advanced
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn delivery_errors_are_distinct() {
        assert_eq!(
            classify_delivery_error_text("40054007 message length exceeded"),
            DeliveryErrorCategory::Length
        );
        assert_eq!(
            classify_delivery_error_text("40054005 消息被去重"),
            DeliveryErrorCategory::Duplicate
        );
        assert_eq!(
            classify_delivery_error_text("40034100 rate limit"),
            DeliveryErrorCategory::Rate
        );
        assert_eq!(
            classify_delivery_error_text("40054013 user 拒收"),
            DeliveryErrorCategory::Permanent
        );
    }
}
