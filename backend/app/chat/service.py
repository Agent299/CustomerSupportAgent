"""Chat turns: persistence, spend cap and events around the query pipeline (docs/PLAN.md §8.6).

    post_message: mask → persist user message → 202
    run_turn (background): ack → spend cap → history → answer() streaming "token" events
                           → persist bot message + cost → "final" or "escalated"

User text is masked with the FULL profile before it is stored or sent to a model; bot text
with SECRETS (it comes from the docs, whose example emails are content, not PII).
"""

import hashlib
import secrets
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from valkey.asyncio import Valkey

from app.chat.events import ChatEvents
from app.genai.llm import Message as LlmMessage
from app.orchestrator.pipeline import AnswerResult, QueryDeps, answer
from app.orchestrator.prompts import FALLBACK
from app.privacy.redact import Profile, redact
from app.tickets.state import BOT_STATES, transition
from shared.db.models import Feedback, LlmUsageDaily, Message, Ticket

log = structlog.get_logger()

HISTORY_MESSAGES = 6  # earlier turns sent with each question
TURN_LOCK_S = 120  # one turn at a time per session; expires if a worker dies mid-turn

HANDOFF = "I've passed your conversation to a support engineer, who will reply here."
ESCALATION_MESSAGES: dict[str, str] = {
    "billing": f"Billing questions are handled by our support team. {HANDOFF}",
    "account": f"Account and deployment actions need a person. {HANDOFF}",
    "security": "Thank you for reporting this. I've passed it to the security team, "
    "who will follow up here.",
    "human_request": HANDOFF,
    "out_of_scope": f"{FALLBACK} {HANDOFF}",
    "invalid_citation": f"I couldn't give you a reliable answer to that. {HANDOFF}",
    "unavailable": f"I'm having trouble answering right now. {HANDOFF}",
}
WAITING_FOR_AGENT = "A support engineer has your conversation and will reply here."
SPEND_CAP_MESSAGE = (
    "The assistant has reached its usage limit for today. Please try again tomorrow."
)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def utc_today() -> date:
    return datetime.now(UTC).date()


class TurnInProgress(Exception):
    pass


@dataclass
class ChatService:
    sessions: async_sessionmaker[AsyncSession]
    kv: Valkey
    events: ChatEvents
    query_deps: Callable[[], QueryDeps]  # a fresh, separately metered LLM per turn
    spend_cap_usd: Decimal

    # ---- sessions ---------------------------------------------------------------------
    async def create_session(self, docs_version: str | None) -> tuple[uuid.UUID, str]:
        token = secrets.token_urlsafe(32)
        ticket = Ticket(id=uuid.uuid4(), token_hash=hash_token(token), docs_version=docs_version)
        async with self.sessions.begin() as session:
            session.add(ticket)
        return ticket.id, token

    async def authorize(self, session: AsyncSession, ticket_id: uuid.UUID, token: str) -> Ticket:
        """The ticket if ``token`` owns it, else ``LookupError`` (callers answer 404)."""
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None or not secrets.compare_digest(ticket.token_hash, hash_token(token)):
            raise LookupError(ticket_id)
        return ticket

    async def check_owner(self, ticket_id: uuid.UUID, token: str) -> None:
        async with self.sessions() as session:
            await self.authorize(session, ticket_id, token)

    # ---- messages ---------------------------------------------------------------------
    async def post_message(
        self, ticket_id: uuid.UUID, token: str, text: str
    ) -> tuple[uuid.UUID, str, str]:
        """Persist the masked user message. Returns (message id, masked text, stream cursor):
        the client reads events after the cursor, so it cannot miss this turn's events."""
        lock = f"turn:{ticket_id}"
        if not await self.kv.set(lock, "1", nx=True, ex=TURN_LOCK_S):
            raise TurnInProgress(ticket_id)
        try:
            masked = redact(text, Profile.FULL)
            message = Message(
                id=uuid.uuid4(), ticket_id=ticket_id, role="user", content_masked=masked
            )
            async with self.sessions.begin() as session:
                await self.authorize(session, ticket_id, token)
                session.add(message)
            cursor = await self.events.last_id(str(ticket_id))
        except BaseException:
            await self.kv.delete(lock)
            raise
        return message.id, masked, cursor

    async def run_turn(
        self, ticket_id: uuid.UUID, message_id: uuid.UUID, text: str, version_hint: str | None
    ) -> None:
        """Answer one user message; always releases the turn lock."""
        sid = str(ticket_id)
        try:
            await self.events.publish(sid, "ack", {"message_id": str(message_id)})
            await self._run_turn(ticket_id, message_id, text, version_hint)
        except Exception:
            log.exception("chat.turn_failed", ticket_id=sid)
            await self._escalate(ticket_id, "unavailable", AnswerResult("ESCALATE", ""))
        finally:
            await self.kv.delete(f"turn:{ticket_id}")

    async def _run_turn(
        self, ticket_id: uuid.UUID, message_id: uuid.UUID, text: str, version_hint: str | None
    ) -> None:
        sid = str(ticket_id)
        async with self.sessions() as session:
            ticket = await session.get(Ticket, ticket_id)
            assert ticket is not None
            status, docs_version = ticket.status, ticket.docs_version
            history = await self._history(session, ticket_id, before=message_id)
        if status not in BOT_STATES:
            await self.events.publish(sid, "escalated", {"message": WAITING_FOR_AGENT})
            return
        if await self.spent_today() >= self.spend_cap_usd:
            log.warning("chat.spend_cap_reached", cap=str(self.spend_cap_usd))
            await self.events.publish(
                sid, "error", {"code": "spend_cap", "message": SPEND_CAP_MESSAGE}
            )
            return

        deps = self.query_deps()

        async def on_token(delta: str) -> None:
            await self.events.publish(sid, "token", {"text": delta})

        result = await answer(
            deps, text, history, version_hint=version_hint or docs_version, on_token=on_token
        )
        cost = Decimal(str(round(getattr(deps.llm, "total_cost_usd", 0.0), 6)))
        model = deps.llm.model_for("answer")
        await self._add_spend(cost)
        if result.route == "ESCALATE":
            reason = result.escalation_reason or "out_of_scope"
            await self._escalate(ticket_id, reason, result, cost, model)
            return

        answer_text = redact(result.answer, Profile.SECRETS)
        bot = self._bot_message(ticket_id, answer_text, result, cost, model)
        async with self.sessions.begin() as session:
            ticket = await session.get(Ticket, ticket_id)
            assert ticket is not None
            transition(ticket, "BOT_ACTIVE")
            session.add(bot)
        citations = [asdict(c) for c in result.citations]
        await self.events.publish(sid, "citations", {"citations": citations})
        await self.events.publish(
            sid,
            "final",
            {
                "message_id": str(bot.id),
                "answer": bot.content_masked,
                "citations": citations,
                "confidence": None,  # calibrated in Phase 4 (ADR 0007)
                "route": "ANSWERED",
                "resolved_version": result.version,
                "first_token_ms": result.first_token_ms,
                "latency_ms": result.latency_ms,
            },
        )

    async def escalate_on_request(self, ticket_id: uuid.UUID, token: str) -> None:
        async with self.sessions() as session:
            ticket = await self.authorize(session, ticket_id, token)
            already = ticket.status not in BOT_STATES
        if already:
            await self.events.publish(str(ticket_id), "escalated", {"message": WAITING_FOR_AGENT})
            return
        await self._escalate(ticket_id, "human_request", AnswerResult("ESCALATE", ""))

    async def _escalate(
        self,
        ticket_id: uuid.UUID,
        reason: str,
        result: AnswerResult,
        cost: Decimal | None = None,
        model: str | None = None,
    ) -> None:
        """Hand off to a person. Text already streamed for this turn is replaced by the
        handoff message (docs/PLAN.md §8.6, streaming vs citations)."""
        text = ESCALATION_MESSAGES.get(reason, HANDOFF)
        bot = self._bot_message(ticket_id, text, result, cost, model)
        bot.escalation_reason = reason
        async with self.sessions.begin() as session:
            ticket = await session.get(Ticket, ticket_id)
            assert ticket is not None
            if ticket.status in BOT_STATES:
                transition(ticket, "ESCALATED", reason=reason)
            session.add(bot)
        await self.events.publish(
            str(ticket_id),
            "escalated",
            {"message_id": str(bot.id), "message": text, "reason": reason},
        )

    def _bot_message(
        self,
        ticket_id: uuid.UUID,
        text: str,
        result: AnswerResult,
        cost: Decimal | None,
        model: str | None,
    ) -> Message:
        return Message(
            id=uuid.uuid4(),
            ticket_id=ticket_id,
            role="bot",
            content_masked=text,
            citations=[asdict(c) for c in result.citations],
            route=result.route,
            escalation_reason=result.escalation_reason,
            model=model,
            cost_usd=cost,
            first_token_ms=result.first_token_ms,
            latency_ms=result.latency_ms or None,
        )

    async def _history(
        self, session: AsyncSession, ticket_id: uuid.UUID, *, before: uuid.UUID
    ) -> list[LlmMessage]:
        rows = (
            await session.scalars(
                select(Message)
                .where(
                    Message.ticket_id == ticket_id,
                    Message.id != before,
                    Message.role.in_(("user", "bot")),
                )
                .order_by(Message.created_at.desc())
                .limit(HISTORY_MESSAGES)
            )
        ).all()
        return [
            {"role": "user" if m.role == "user" else "assistant", "content": m.content_masked}
            for m in reversed(rows)
        ]

    # ---- transcript and feedback ------------------------------------------------------
    async def transcript(self, ticket_id: uuid.UUID, token: str) -> tuple[Ticket, list[Message]]:
        async with self.sessions() as session:
            ticket = await self.authorize(session, ticket_id, token)
            messages = (
                await session.scalars(
                    select(Message)
                    .where(Message.ticket_id == ticket_id)
                    .order_by(Message.created_at)
                )
            ).all()
        return ticket, list(messages)

    async def add_feedback(
        self, message_id: uuid.UUID, token: str, rating: int, comment: str | None
    ) -> None:
        async with self.sessions.begin() as session:
            message = await session.get(Message, message_id)
            if message is None or message.role != "bot":
                raise LookupError(message_id)
            await self.authorize(session, message.ticket_id, token)
            masked = redact(comment, Profile.FULL) if comment else None
            await session.execute(
                insert(Feedback)
                .values(message_id=message_id, rating=rating, comment=masked)
                .on_conflict_do_update(
                    index_elements=[Feedback.message_id],
                    set_={"rating": rating, "comment": masked, "created_at": func.now()},
                )
            )

    # ---- spend ------------------------------------------------------------------------
    async def spent_today(self) -> Decimal:
        async with self.sessions() as session:
            row = await session.get(LlmUsageDaily, utc_today())
        return row.cost_usd if row else Decimal(0)

    async def _add_spend(self, cost: Decimal) -> None:
        if cost <= 0:
            return
        async with self.sessions.begin() as session:
            await session.execute(
                insert(LlmUsageDaily)
                .values(day=utc_today(), cost_usd=cost)
                .on_conflict_do_update(
                    index_elements=[LlmUsageDaily.day],
                    set_={"cost_usd": LlmUsageDaily.cost_usd + cost},
                )
            )
