"""Deterministic PII / secret redaction (docs/PLAN.md §5, §8.9). No ML models.

Two profiles:
- ``SECRETS``: credentials, API keys, payment cards. Applied to public docs at ingestion,
  where illustrative emails/IPs are content, not personal data.
- ``FULL``: SECRETS plus emails and phone numbers. Applied to user messages, tickets and
  anything sent to an LLM or written to logs.
"""

import re
from collections.abc import Callable
from enum import Enum


class Profile(Enum):
    SECRETS = "secrets"
    FULL = "full"


# mongodb://user:password@host or mongodb+srv://user:password@host
_CONN_STRING_CREDS = re.compile(r"(mongodb(?:\+srv)?://)([^:/@\s]+):([^@/\s]+)@", re.IGNORECASE)
_PLACEHOLDER_PASSWORD = re.compile(r"^<[^>]+>$|^\$\{?[A-Z_]+\}?$|^\*+$")

_API_KEYS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),  # OpenAI
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),  # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),  # GitHub tokens
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),  # Slack tokens
)
_CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<![\w.])\+?\d{1,3}?[ .-]?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w.])")

CONNECTION_PASSWORD_MASK = "<PASSWORD_MASKED>"  # noqa: S105 - a mask, not a secret
API_KEY_MASK = "<API_KEY_MASKED>"
CARD_MASK = "<CARD_MASKED>"
EMAIL_MASK = "<EMAIL_MASKED>"
PHONE_MASK = "<PHONE_MASKED>"


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _mask_connection_password(match: re.Match[str]) -> str:
    scheme, user, password = match.group(1), match.group(2), match.group(3)
    if _PLACEHOLDER_PASSWORD.match(password):
        return match.group(0)
    return f"{scheme}{user}:{CONNECTION_PASSWORD_MASK}@"


def _mask_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if 13 <= len(digits) <= 19 and _luhn_valid(digits):
        return CARD_MASK
    return match.group(0)


def _redact_secrets(text: str) -> str:
    text = _CONN_STRING_CREDS.sub(_mask_connection_password, text)
    for pattern in _API_KEYS:
        text = pattern.sub(API_KEY_MASK, text)
    return _CARD_CANDIDATE.sub(_mask_card, text)


def _redact_full(text: str) -> str:
    text = _redact_secrets(text)
    text = _EMAIL.sub(EMAIL_MASK, text)
    return _PHONE.sub(PHONE_MASK, text)


_REDACTORS: dict[Profile, Callable[[str], str]] = {
    Profile.SECRETS: _redact_secrets,
    Profile.FULL: _redact_full,
}


def redact(text: str, profile: Profile = Profile.FULL) -> str:
    return _REDACTORS[profile](text)
