"""Deterministic pre-checks (docs/PLAN.md §8.6 T1, §5): no model call.

``escalation_reason`` routes requests the docs cannot answer (billing, account actions,
security reports, asking for a human) straight to a person. Off-topic technical questions
are not caught here: they reach the answer model, whose fallback reply escalates them.
"""

import re
from typing import Literal

EscalationReason = Literal[
    "billing",
    "account",
    "security",
    "human_request",
    "out_of_scope",  # the model found nothing in the docs that answers the question
    "invalid_citation",  # the answer cited something it was not given
    "unavailable",  # the model or search failed
]

SUPPORTED_VERSIONS = ("9.0", "8.0", "7.0")
DEFAULT_VERSION = "9.0"

_RULES: tuple[tuple[EscalationReason, re.Pattern[str]], ...] = (
    (
        "security",
        re.compile(
            r"\b(security (vulnerability|issue|flaw|hole|incident)|vulnerabilit(y|ies)"
            r"|data breach|been (hacked|breached|compromised))\b"
        ),
    ),
    (
        "account",
        re.compile(
            r"\b(locked out|can'?t (log|sign) ?in to (my|our)|(reset|verification|confirmation) "
            r"(email|link)|(delete|close) (my|our) (atlas )?account|my personal data|gdpr"
            r"|(can|could|would|will) you (please )?(restart|reboot|scale|resize|upgrade|access"
            r"|log ?in to|look into) (my|our) (production |prod |atlas )?(cluster|database"
            r"|deployment|server|instance|account|project|organization|org))\b"
        ),
    ),
    (
        "billing",
        re.compile(
            r"\b(billing|billed|invoices?|refunds?|charged (me|us|twice)|overcharged"
            r"|payment (method|failed|issue)|credit card"
            r"|subscription|pricing (contract|agreement|plan)|negotiate|discount|quote for)\b"
        ),
    ),
    (
        "human_request",
        re.compile(
            r"\b((real|actual|live) (person|human|agent)|human (agent|being|support)"
            r"|(talk|speak|chat) (to|with) (a |an |someone|somebody)?\s*(human|person|agent"
            r"|representative|engineer|someone|somebody)"
            r"|get me (someone|somebody|a human|a person|an agent))\b"
        ),
    ),
)

_VERSION = re.compile(
    r"\b(?:mongodb|mongo|version|v)\s*v?(9|8|7)(?:\.0)?(?:\.\d+)?\b"
    r"|\b(9|8|7)\.0(?:\.\d+)?\b",
    re.IGNORECASE,
)


def escalation_reason(text: str) -> EscalationReason | None:
    lowered = text.lower()
    for reason, pattern in _RULES:
        if pattern.search(lowered):
            return reason
    return None


def detect_version(texts: list[str]) -> str | None:
    """The docs version the user mentions; the latest mention wins. ``None`` = not stated."""
    for text in reversed(texts):
        matches = list(_VERSION.finditer(text))
        if matches:
            major = matches[-1].group(1) or matches[-1].group(2)
            return f"{major}.0"
    return None
