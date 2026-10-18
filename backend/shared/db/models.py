"""Relational schema (docs/PLAN.md §8.4). Phase 1: knowledge base. Phase 2: chat."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Document(Base):
    """One docs page in one version, e.g. ``9.0:core/index-unique``."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("doc_key", "version", name="uq_documents_key_version"),
        Index("ix_documents_version_status", "version", "status"),
    )

    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    doc_key: Mapped[str] = mapped_column(String(480))
    version: Mapped[str] = mapped_column(String(16))
    url: Mapped[str] = mapped_column(Text)
    markdown_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(16), default="DOC")
    security_level: Mapped[str] = mapped_column(String(16), default="PUBLIC")
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | deleted
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Chunk(Base):
    """Version-independent chunk; shared by every page/version that contains it (ADR 0012)."""

    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    breadcrumb: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    links: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Set by the index worker; NULL = never indexed. A different embedding_model than the
    # configured one means the chunk must be re-embedded.
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    embedding_model: Mapped[str | None] = mapped_column(String(64))


class ChunkOccurrence(Base):
    """Where a chunk appears: (document, position). Its versions come from the document."""

    __tablename__ = "chunk_occurrences"
    __table_args__ = (Index("ix_chunk_occurrences_chunk_id", "chunk_id"),)

    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunks.id", ondelete="CASCADE"))


class IdentifierAnchor(Base):
    """Identifier → page it anchors, per version (workers/ingest/anchors.py)."""

    __tablename__ = "identifier_anchors"

    version: Mapped[str] = mapped_column(String(16), primary_key=True)
    identifier: Mapped[str] = mapped_column(String(128), primary_key=True)
    doc_key: Mapped[str] = mapped_column(String(480), primary_key=True)
    score: Mapped[float] = mapped_column(Float)


class IngestOutbox(Base):
    """Webhook requests waiting to be published to ``raw_documents`` by the next run.

    The API never talks to Kafka directly (transactional outbox); the relay worker
    publishes pending rows.
    """

    __tablename__ = "ingest_outbox"
    __table_args__ = (
        Index(
            "ix_ingest_outbox_unpublished",
            "id",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IngestRun(Base):
    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|success|failed
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class Ticket(Base):
    """One support conversation (a chat session) and its state (docs/PLAN.md §8.7).

    Until Phase 4 adds login, a session is owned by whoever holds its token; only the
    token's hash is stored.
    """

    __tablename__ = "tickets"
    __table_args__ = (Index("ix_tickets_status", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="OPEN")
    version: Mapped[int] = mapped_column(Integer, default=1)  # optimistic lock
    docs_version: Mapped[str | None] = mapped_column(String(16))
    escalation_reason: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012 - SQLAlchemy convention


class Message(Base):
    """Append-only transcript. Content is PII-masked before it is stored."""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'bot', 'agent', 'system')", name="ck_messages_role"),
        Index("ix_messages_ticket_created", "ticket_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(8))
    content_masked: Mapped[str] = mapped_column(Text)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    route: Mapped[str | None] = mapped_column(String(16))  # bot: ANSWERED | ESCALATE
    escalation_reason: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    first_token_ms: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (CheckConstraint("rating IN (-1, 1)", name="ck_feedback_rating"),)

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    rating: Mapped[int] = mapped_column(SmallInteger)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LlmUsageDaily(Base):
    """Spend per UTC day; backs the global spend cap (docs/PLAN.md §8.8)."""

    __tablename__ = "llm_usage_daily"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal(0))
