"""Postgres persistence for ingestion: documents, chunks, occurrences, outbox."""

from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import delete, distinct, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Chunk, ChunkOccurrence, Document, IngestOutbox
from shared.sources import DocVersion, document_id, markdown_url, page_url
from workers.ingest.chunker import ParsedPage


@dataclass(frozen=True)
class PageWrite:
    chunk_ids: list[str]  # the page's current chunks (unique, in page order)
    removed_chunk_ids: list[str]  # chunks the page referenced before but no longer does
    new_chunk_ids: list[str]  # chunks that did not exist anywhere before


@dataclass(frozen=True)
class ChunkForIndex:
    id: str
    breadcrumb: str
    text: str
    token_count: int
    indexed_at: datetime | None
    embedding_model: str | None
    versions: list[str]  # versions of *active* documents containing the chunk
    doc_keys: list[str]
    titles: list[str]


async def content_hash(session: AsyncSession, version: DocVersion, key: str) -> str | None:
    """Hash of the active document, or None if unknown/deleted."""
    return await session.scalar(
        select(Document.content_hash).where(
            Document.id == document_id(key, version), Document.status == "active"
        )
    )


async def active_keys(session: AsyncSession, version: DocVersion) -> set[str]:
    rows = await session.scalars(
        select(Document.doc_key).where(
            Document.version == version.label, Document.status == "active"
        )
    )
    return set(rows)


async def _occurrence_chunk_ids(session: AsyncSession, doc_id: str) -> list[str]:
    rows = await session.scalars(
        select(ChunkOccurrence.chunk_id)
        .where(ChunkOccurrence.document_id == doc_id)
        .order_by(ChunkOccurrence.ordinal)
    )
    return list(dict.fromkeys(rows))


async def upsert_page(
    session: AsyncSession,
    *,
    key: str,
    version: DocVersion,
    parsed: ParsedPage,
    content_hash: str,
) -> PageWrite:
    """Replace one page's chunk list. Shared chunks are inserted once (ON CONFLICT DO NOTHING)."""
    doc_id = document_id(key, version)
    previous = await _occurrence_chunk_ids(session, doc_id)
    values = {
        "id": doc_id,
        "doc_key": key,
        "version": version.label,
        "url": page_url(key, version),
        "markdown_url": markdown_url(key, version),
        "title": parsed.title or key,
        "source_type": "DOC",
        "security_level": "PUBLIC",
        "content_hash": content_hash,
        "status": "active",
    }
    await session.execute(
        insert(Document)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[Document.id],
            set_={**values, "fetched_at": func.now(), "updated_at": func.now()},
        )
    )
    await session.execute(delete(ChunkOccurrence).where(ChunkOccurrence.document_id == doc_id))

    current = list(dict.fromkeys(chunk.id for chunk in parsed.chunks))
    new_chunk_ids: list[str] = []
    if parsed.chunks:
        unique = {chunk.id: chunk for chunk in parsed.chunks}
        inserted = await session.execute(
            insert(Chunk)
            .values(
                [
                    {
                        "id": chunk.id,
                        "breadcrumb": chunk.breadcrumb_text,
                        "text": chunk.text,
                        "token_count": chunk.token_count,
                        "links": [asdict(link) for link in chunk.links],
                    }
                    for chunk in unique.values()
                ]
            )
            .on_conflict_do_nothing(index_elements=[Chunk.id])
            .returning(Chunk.id)
        )
        new_chunk_ids = list(inserted.scalars())
        await session.execute(
            insert(ChunkOccurrence).values(
                [
                    {"document_id": doc_id, "ordinal": chunk.ordinal, "chunk_id": chunk.id}
                    for chunk in parsed.chunks
                ]
            )
        )
    removed = [chunk_id for chunk_id in previous if chunk_id not in set(current)]
    return PageWrite(chunk_ids=current, removed_chunk_ids=removed, new_chunk_ids=new_chunk_ids)


async def mark_deleted(session: AsyncSession, version: DocVersion, key: str) -> list[str] | None:
    """Mark a page deleted. Returns the chunk ids it referenced, or None if it wasn't active."""
    doc_id = document_id(key, version)
    result = await session.execute(
        update(Document)
        .where(Document.id == doc_id, Document.status != "deleted")
        .values(status="deleted", updated_at=func.now())
    )
    if not result.rowcount:  # type: ignore[attr-defined]
        return None
    removed = await _occurrence_chunk_ids(session, doc_id)
    await session.execute(delete(ChunkOccurrence).where(ChunkOccurrence.document_id == doc_id))
    return removed


async def delete_orphan_chunks(session: AsyncSession) -> list[str]:
    """Remove chunks no page references any more; returns their ids (to purge from indexes)."""
    referenced = select(ChunkOccurrence.chunk_id).where(ChunkOccurrence.chunk_id == Chunk.id)
    result = await session.execute(delete(Chunk).where(~referenced.exists()).returning(Chunk.id))
    return list(result.scalars())


async def chunks_for_index(session: AsyncSession, chunk_ids: list[str]) -> list[ChunkForIndex]:
    """Chunks that still exist, with the versions/pages of their active documents."""
    if not chunk_ids:
        return []
    active = Document.status == "active"
    query = (
        select(
            Chunk.id,
            Chunk.breadcrumb,
            Chunk.text,
            Chunk.token_count,
            Chunk.indexed_at,
            Chunk.embedding_model,
            func.array_agg(distinct(Document.version)).filter(active).label("versions"),
            func.array_agg(distinct(Document.doc_key)).filter(active).label("doc_keys"),
            func.array_agg(distinct(Document.title)).filter(active).label("titles"),
        )
        .outerjoin(ChunkOccurrence, ChunkOccurrence.chunk_id == Chunk.id)
        .outerjoin(Document, Document.id == ChunkOccurrence.document_id)
        .where(Chunk.id.in_(chunk_ids))
        .group_by(Chunk.id)
    )
    rows = await session.execute(query)
    return [
        ChunkForIndex(
            id=row.id,
            breadcrumb=row.breadcrumb,
            text=row.text,
            token_count=row.token_count,
            indexed_at=row.indexed_at,
            embedding_model=row.embedding_model,
            versions=sorted(row.versions or []),
            doc_keys=sorted(row.doc_keys or []),
            titles=sorted(row.titles or []),
        )
        for row in rows
    ]


async def chunks_needing_embedding(
    session: AsyncSession, embedding_model: str, limit: int
) -> list[str]:
    rows = await session.scalars(
        select(Chunk.id)
        .where(or_(Chunk.indexed_at.is_(None), Chunk.embedding_model != embedding_model))
        .order_by(Chunk.id)
        .limit(limit)
    )
    return list(rows)


async def mark_indexed(session: AsyncSession, chunk_ids: list[str], embedding_model: str) -> None:
    if chunk_ids:
        await session.execute(
            update(Chunk)
            .where(Chunk.id.in_(chunk_ids))
            .values(indexed_at=func.now(), embedding_model=embedding_model)
        )


async def pending_outbox(session: AsyncSession, limit: int = 500) -> list[IngestOutbox]:
    rows = await session.scalars(
        select(IngestOutbox)
        .where(IngestOutbox.published_at.is_(None))
        .order_by(IngestOutbox.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(rows)


async def mark_published(session: AsyncSession, outbox_ids: list[int]) -> None:
    if outbox_ids:
        await session.execute(
            update(IngestOutbox)
            .where(IngestOutbox.id.in_(outbox_ids))
            .values(published_at=func.now())
        )
