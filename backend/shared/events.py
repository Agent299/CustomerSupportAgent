"""Ingestion event contracts (docs/PLAN.md §8.3). Topics are keyed by document so a page's
events stay ordered within a partition."""

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

TOPIC_RAW_DOCUMENTS = "raw_documents"
TOPIC_CHUNKED_DOCUMENTS = "chunked_documents"
DLQ_SUFFIX = ".dlq"
ALL_TOPICS = (TOPIC_RAW_DOCUMENTS, TOPIC_CHUNKED_DOCUMENTS)

Op = Literal["UPSERT", "DELETE"]


def _now() -> datetime:
    return datetime.now(UTC)


class Event(BaseModel):
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    emitted_at: datetime = Field(default_factory=_now)


class RawDocumentEvent(Event):
    """A docs page should be (re)fetched, or removed."""

    source_type: Literal["DOC"] = "DOC"
    version: str
    key: str
    op: Op

    @property
    def partition_key(self) -> str:
        return f"{self.version}:{self.key}"


class ChunkedDocumentEvent(Event):
    """A page's chunk list changed in Postgres; the index must be brought in line."""

    document_id: str
    op: Op
    chunk_ids: list[str] = Field(default_factory=list)  # current chunks of the page
    removed_chunk_ids: list[str] = Field(default_factory=list)  # chunks the page no longer has

    @property
    def partition_key(self) -> str:
        return self.document_id


class DeadLetter(BaseModel):
    topic: str
    key: str
    payload: str
    error: str
    failed_at: datetime = Field(default_factory=_now)
