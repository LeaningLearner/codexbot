from __future__ import annotations

from codexbot.security import (
    hash_pairing_code,
    normalize_pairing_code,
    prompt_preview,
    redact_secrets,
)


def test_redacts_common_secret_shapes() -> None:
    source = (
        "sk-abcdefghijklmnopqrstuvwxyz "
        "Bearer abc.def-123 "
        "api_key=super-secret appSecret:qq-secret password=hunter2"
    )

    result = redact_secrets(source)

    assert "abcdefghijklmnopqrstuvwxyz" not in result
    assert "abc.def-123" not in result
    assert "super-secret" not in result
    assert "qq-secret" not in result
    assert "hunter2" not in result
    assert result.count("[REDACTED]") == 5


def test_redacts_json_secret_values_without_leaking_quoted_contents() -> None:
    source = (
        '{"api_key": "json-api-secret,with punctuation", '
        '"appSecret":"json-app-secret", '
        '"access_token": "json-access-token", '
        '"client_secret": "json-client-secret"}'
    )

    result = redact_secrets(source)

    assert "json-api-secret" not in result
    assert "json-app-secret" not in result
    assert "json-access-token" not in result
    assert "json-client-secret" not in result
    assert result.count("[REDACTED]") == 4
    assert '"api_key": "[REDACTED]"' in result


def test_prompt_preview_is_compact_bounded_and_emoji_safe() -> None:
    source = "  请处理\n\n" + ("资料👨‍👩‍👧‍👦 " * 40) + "token=do-not-store"

    result = prompt_preview(source, 120)

    assert len(result) <= 120
    assert "\n" not in result
    assert result.endswith("…")
    assert not result.endswith("\u200d…")
    assert not result.endswith(("\ufe0e…", "\ufe0f…"))


def test_pairing_codes_are_case_and_separator_insensitive() -> None:
    assert normalize_pairing_code("abcd-ef23") == "ABCDEF23"
    assert hash_pairing_code("ABCD-EF23") == hash_pairing_code("abcd ef23")

