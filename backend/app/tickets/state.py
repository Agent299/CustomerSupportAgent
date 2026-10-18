"""Ticket state machine (docs/PLAN.md §8.7). Every status change goes through ``transition``.

    OPEN ──► BOT_ACTIVE ──► RESOLVED_BY_BOT ──► CLOSED
                  │   └──(idle 30 min)──► ABANDONED
                  └──► ESCALATED ──► AGENT_ACTIVE ──► RESOLVED ──► CLOSED

Concurrent writers are caught by optimistic locking on ``tickets.version`` (the ORM's
``version_id_col``): the losing commit raises ``StaleDataError``.
"""

from typing import Literal

from sqlalchemy import func

from shared.db.models import Ticket

Status = Literal[
    "OPEN",
    "BOT_ACTIVE",
    "RESOLVED_BY_BOT",
    "ESCALATED",
    "AGENT_ACTIVE",
    "RESOLVED",
    "ABANDONED",
    "CLOSED",
]

ALLOWED: dict[Status, frozenset[Status]] = {
    "OPEN": frozenset({"BOT_ACTIVE", "ESCALATED", "ABANDONED"}),
    "BOT_ACTIVE": frozenset({"RESOLVED_BY_BOT", "ESCALATED", "ABANDONED"}),
    "RESOLVED_BY_BOT": frozenset({"CLOSED", "BOT_ACTIVE"}),
    "ESCALATED": frozenset({"AGENT_ACTIVE"}),
    "AGENT_ACTIVE": frozenset({"RESOLVED"}),
    "RESOLVED": frozenset({"CLOSED"}),
    "ABANDONED": frozenset(),
    "CLOSED": frozenset(),
}

# Where the bot still answers user messages; elsewhere they wait for a person.
BOT_STATES: frozenset[str] = frozenset({"OPEN", "BOT_ACTIVE", "RESOLVED_BY_BOT"})


class InvalidTransition(Exception):
    pass


def transition(ticket: Ticket, to: Status, *, reason: str | None = None) -> None:
    """Change status in the session; the caller commits. Same-state is a no-op."""
    current: Status = ticket.status  # type: ignore[assignment]
    if current == to:
        return
    if to not in ALLOWED[current]:
        raise InvalidTransition(f"{current} -> {to}")
    ticket.status = to
    ticket.updated_at = func.now()
    if to == "ESCALATED":
        ticket.escalation_reason = reason
