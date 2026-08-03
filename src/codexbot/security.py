from __future__ import annotations

import hashlib
import re
import secrets
import string
import unicodedata
from dataclasses import dataclass


PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

_SECRET_KEY_NAMES = (
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"app[_-]?secret|client[_-]?secret|password|passwd|token|secret)"
)
_JSON_SECRET_PATTERN = re.compile(
    rf'(?P<prefix>"{_SECRET_KEY_NAMES}"\s*:\s*")'
    r'(?P<value>(?:\\.|[^"\\])*)(?P<suffix>")',
    re.IGNORECASE,
)

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/]+=*"),
    re.compile(
        rf"\b({_SECRET_KEY_NAMES})(\s*[:=]\s*)([^\s,;]+)",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True)
class Credentials:
    app_id: str
    app_secret: str


def redact_secrets(text: str) -> str:
    result = _JSON_SECRET_PATTERN.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]{match.group('suffix')}",
        text,
    )
    result = _SECRET_PATTERNS[0].sub("[REDACTED]", result)
    result = _SECRET_PATTERNS[1].sub("Bearer [REDACTED]", result)
    result = _SECRET_PATTERNS[2].sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", result)
    return result


def prompt_preview(text: str, limit: int = 120) -> str:
    normalized = " ".join((text or "").split())
    normalized = redact_secrets(normalized)
    if len(normalized) <= limit:
        return normalized
    cut = max(1, limit - 1)
    while cut > 1 and cut < len(normalized):
        current = normalized[cut]
        previous = normalized[cut - 1]
        if (
            unicodedata.combining(current)
            or current == "\u200d"
            or previous == "\u200d"
            or current in {"\ufe0e", "\ufe0f"}
            or previous in {"\ufe0e", "\ufe0f"}
        ):
            cut -= 1
            continue
        break
    return normalized[:cut].rstrip() + "…"


def generate_pairing_code() -> str:
    raw = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_pairing_code(code: str) -> str:
    return "".join(char for char in code.upper() if char in string.ascii_uppercase + string.digits)


def hash_pairing_code(code: str) -> str:
    normalized = normalize_pairing_code(code)
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def store_credentials(app_id: str, app_secret: str) -> None:
    import keyring

    app_id = app_id.strip()
    app_secret = app_secret.strip()
    if not app_id or not app_secret:
        raise ValueError("AppID 和 AppSecret 均不能为空")
    keyring.set_password("CodexBot.QQ", "app_id", app_id)
    keyring.set_password("CodexBot.QQ", "app_secret", app_secret)


def load_credentials() -> Credentials | None:
    import keyring

    app_id = keyring.get_password("CodexBot.QQ", "app_id")
    app_secret = keyring.get_password("CodexBot.QQ", "app_secret")
    if not app_id or not app_secret:
        return None
    return Credentials(app_id=app_id, app_secret=app_secret)
