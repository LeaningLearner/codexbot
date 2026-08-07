//! Unicode-safe QQ message segmentation.

use std::error::Error;
use std::fmt;
use unicode_normalization::char::canonical_combining_class;

pub const DEFAULT_CHUNK_SIZE: usize = 1_500;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FormattingError {
    LimitTooSmall,
    IndexOutOfBounds(usize),
    SegmentTooShort,
}

impl fmt::Display for FormattingError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::LimitTooSmall => f.write_str("limit must be at least 80"),
            Self::IndexOutOfBounds(index) => write!(f, "segment index out of bounds: {index}"),
            Self::SegmentTooShort => f.write_str("segment cannot be split further"),
        }
    }
}

impl Error for FormattingError {}

fn unsafe_boundary_character(value: char) -> bool {
    canonical_combining_class(value) != 0 || matches!(value, '\u{200d}' | '\u{fe0e}' | '\u{fe0f}')
}

fn safe_cut(characters: &[char], target: usize) -> usize {
    let mut target = target.clamp(1, characters.len());
    while target > 1 && target < characters.len() {
        let current = characters[target];
        let previous = characters[target - 1];
        if unsafe_boundary_character(current)
            || current == '\u{200d}'
            || previous == '\u{200d}'
            || matches!(current, '\u{fe0e}' | '\u{fe0f}')
            || matches!(previous, '\u{fe0e}' | '\u{fe0f}')
        {
            target -= 1;
        } else {
            break;
        }
    }
    target
}

fn rfind_in(characters: &[char], needle: char, start: usize, end: usize) -> Option<usize> {
    characters
        .get(start..end)
        .and_then(|slice| slice.iter().rposition(|value| *value == needle))
        .map(|position| start + position)
}

pub fn split_text(text: &str, limit: usize) -> Result<Vec<String>, FormattingError> {
    if text.is_empty() {
        return Ok(vec![String::new()]);
    }
    if limit < 80 {
        return Err(FormattingError::LimitTooSmall);
    }

    let payload_limit = limit - 20;
    let characters: Vec<char> = text.chars().collect();
    let mut chunks = Vec::new();
    let mut offset = 0;
    while characters.len() - offset > payload_limit {
        let remaining = &characters[offset..];
        let mut cut = safe_cut(remaining, payload_limit);
        if let Some(newline) =
            rfind_in(remaining, '\n', cut.saturating_sub(300), cut).filter(|p| *p > 0)
        {
            cut = newline + 1;
        } else if let Some(space) =
            rfind_in(remaining, ' ', cut.saturating_sub(120), cut).filter(|p| *p > 0)
        {
            cut = space + 1;
        }
        chunks.push(remaining[..cut].iter().collect());
        offset += cut;
    }
    chunks.push(characters[offset..].iter().collect());
    Ok(chunks)
}

pub fn split_text_default(text: &str) -> Vec<String> {
    // The default is a valid compile-time constant, so this cannot fail.
    split_text(text, DEFAULT_CHUNK_SIZE).expect("default chunk size is valid")
}

pub fn render_segment(segments: &[String], index: usize) -> Result<String, FormattingError> {
    let segment = segments
        .get(index)
        .ok_or(FormattingError::IndexOutOfBounds(index))?;
    if segments.len() == 1 {
        return Ok(segment.clone());
    }
    Ok(format!("[{}/{}]\n{}", index + 1, segments.len(), segment))
}

pub fn bisect_segment(segment: &str) -> Result<(String, String), FormattingError> {
    let characters: Vec<char> = segment.chars().collect();
    if characters.len() < 2 {
        return Err(FormattingError::SegmentTooShort);
    }
    let mut cut = safe_cut(&characters, characters.len() / 2);
    let start = cut.saturating_sub(150).max(1);
    let end = (cut + 150).min(characters.len());
    if let Some(newline) = rfind_in(&characters, '\n', start, end).filter(|p| *p > 0) {
        cut = newline + 1;
    }
    Ok((
        characters[..cut].iter().collect(),
        characters[cut..].iter().collect(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splitting_preserves_every_scalar_and_safe_boundaries() {
        let source = format!("{}结束", "中文🙂👨‍👩‍👧‍👦e\u{301}\n\n下一行 ".repeat(100));
        let chunks = split_text(&source, 120).unwrap();
        assert_eq!(chunks.concat(), source);
        assert!(chunks.len() > 2);
        for (index, chunk) in chunks.iter().enumerate() {
            assert!(render_segment(&chunks, index).unwrap().chars().count() <= 120);
            if index > 0 {
                let first = chunk.chars().next().unwrap();
                assert!(!unsafe_boundary_character(first));
            }
            if index + 1 < chunks.len() {
                assert_ne!(chunk.chars().last(), Some('\u{200d}'));
            }
        }
    }

    #[test]
    fn bisection_is_lossless() {
        let source = format!("{}\n\n{}", "甲".repeat(100), "乙🙂".repeat(100));
        let (left, right) = bisect_segment(&source).unwrap();
        assert_eq!(left + &right, source);
        assert!(!right.is_empty());
    }
}
