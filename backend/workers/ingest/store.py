"""Idempotent persistence of parsed pages into Postgres (documents, chunks, occurrences)."""

from dataclasses import asdict, dataclass

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Chunk, ChunkOccurrence, Document
from shared.sources import DocVersion, document_id, markdown_url, page_url
from workers.ingest.chunker import ParsedPage


@dataclass(frozen=True)
class PageWrite:
    new_chunk_ids: list[str]  # chunks that did not exist before → need embedding/indexing


async def content_hashes(session: AsyncSession, version: DocVersion) -> dict[str, str]:
    """doc_key → content_hash for this version's active pages."""
    rows = await session.execute(
        select(Document.doc_key, Document.content_hash).where(
            Document.version == version.label, Document.status == "active"
        )
    )
    return {key: content_hash for key, content_hash in rows}


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
    return PageWrite(new_chunk_ids=new_chunk_ids)


async def mark_deleted(session: AsyncSession, version: DocVersion, keys: list[str]) -> int:
    if not keys:
        return 0
    ids = [document_id(key, version) for key in keys]
    await session.execute(delete(ChunkOccurrence).where(ChunkOccurrence.document_id.in_(ids)))
    result = await session.execute(
        update(Document)
        .where(Document.id.in_(ids), Document.status != "deleted")
        .values(status="deleted", updated_at=func.now())
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


async def delete_orphan_chunks(session: AsyncSession) -> list[str]:
    """Remove chunks no page references any more; returns their ids (to purge from indexes)."""
    orphans = select(ChunkOccurrence.chunk_id).where(ChunkOccurrence.chunk_id == Chunk.id)
    result = await session.execute(delete(Chunk).where(~orphans.exists()).returning(Chunk.id))
    return list(result.scalars())
