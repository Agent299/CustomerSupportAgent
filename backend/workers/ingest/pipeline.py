"""Event-driven ingestion stages (docs/PLAN.md §6, §8.5).

    schedule ──raw_documents──▶ chunk stage ──chunked_documents──▶ index stage
    (sitemap diff + outbox)     (fetch, redact,                     (embed new chunks,
                                 chunk, Postgres)                    OpenSearch upsert/
                                                                     metadata/delete)

Every handler is idempotent, so at-least-once delivery and re-runs are safe.
"""

import asyncio
import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.genai.embeddings import Embedder, embedding_input
from app.privacy.redact import Profile, redact
from app.retrieval.kb_index import KbIndex
from shared.bus import EventBus
from shared.events import (
    TOPIC_CHUNKED_DOCUMENTS,
    TOPIC_RAW_DOCUMENTS,
    ChunkedDocumentEvent,
    RawDocumentEvent,
)
from shared.sources import DocVersion, document_id, get_version
from workers.ingest.anchors import rebuild_anchors
from workers.ingest.chunker import TokenCounter, chunk_markdown, default_token_counter
from workers.ingest.crawler import DocsCrawler
from workers.ingest.store import (
    ChunkForIndex,
    active_keys,
    chunks_for_index,
    chunks_needing_embedding,
    content_hash,
    delete_orphan_chunks,
    mark_deleted,
    mark_indexed,
    mark_published,
    pending_outbox,
    upsert_page,
)

log = structlog.get_logger()

CHUNK_GROUP = "chunk-worker"
INDEX_GROUP = "index-worker"


def page_hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode()).hexdigest()


@dataclass
class Pipeline:
    bus: EventBus
    sessions: async_sessionmaker[AsyncSession]
    crawler: DocsCrawler
    index: KbIndex
    embedder: Embedder
    versions: tuple[DocVersion, ...]
    count_tokens: TokenCounter = default_token_counter
    stats: Counter[str] = field(default_factory=Counter)

    # ---- schedule ---------------------------------------------------------------------
    async def schedule(self, *, limit: int | None = None) -> None:
        """Emit raw_documents events: every sitemap page (hash check happens downstream),
        deletions for pages gone from the sitemap, and pending webhook outbox rows."""
        await self.crawler.load_robots()
        for version in self.versions:
            keys = await self.crawler.discover(version)
            if limit is not None:
                keys = keys[:limit]  # smoke runs never infer deletions
            else:
                async with self.sessions() as session:
                    gone = sorted(await active_keys(session, version) - set(keys))
                for key in gone:
                    await self._emit_raw(version, key, "DELETE")
            for key in keys:
                await self._emit_raw(version, key, "UPSERT")
            log.info("ingest.scheduled", version=version.label, pages=len(keys))
        await self.drain_outbox()

    async def drain_outbox(self) -> None:
        async with self.sessions.begin() as session:
            rows = await pending_outbox(session)
            for row in rows:
                event = RawDocumentEvent.model_validate(row.payload)
                await self.bus.publish(TOPIC_RAW_DOCUMENTS, event.partition_key, event)
            await mark_published(session, [row.id for row in rows])
        self._count("outbox:published", len(rows))

    async def _emit_raw(self, version: DocVersion, key: str, op: str) -> None:
        event = RawDocumentEvent(version=version.label, key=key, op=op)  # type: ignore[arg-type]
        await self.bus.publish(TOPIC_RAW_DOCUMENTS, event.partition_key, event)
        self._count(f"scheduled:{op.lower()}")

    # ---- chunk stage ------------------------------------------------------------------
    async def handle_raw(self, payload: str) -> None:
        event = RawDocumentEvent.model_validate_json(payload)
        version = get_version(event.version, self.versions)
        if event.op == "DELETE":
            await self._delete_page(version, event.key)
            return
        page = await self.crawler.fetch(event.key, version)
        if page.markdown is None:
            await self._delete_page(version, event.key)
            return
        markdown = redact(page.markdown, Profile.SECRETS)
        digest = page_hash(markdown)
        async with self.sessions() as session:
            known = await content_hash(session, version, event.key)
        if known == digest:
            self._count(f"{version.label}:unchanged")
            return
        parsed = await asyncio.to_thread(
            chunk_markdown, markdown, fallback_title=event.key, count_tokens=self.count_tokens
        )
        async with self.sessions.begin() as session:
            written = await upsert_page(
                session, key=event.key, version=version, parsed=parsed, content_hash=digest
            )
        self._count(f"{version.label}:{'updated' if known else 'added'}")
        self._count("chunks:new", len(written.new_chunk_ids))
        doc_id = document_id(event.key, version)
        await self.bus.publish(
            TOPIC_CHUNKED_DOCUMENTS,
            doc_id,
            ChunkedDocumentEvent(
                document_id=doc_id,
                op="UPSERT",
                chunk_ids=written.chunk_ids,
                removed_chunk_ids=written.removed_chunk_ids,
            ),
        )

    async def _delete_page(self, version: DocVersion, key: str) -> None:
        async with self.sessions.begin() as session:
            removed = await mark_deleted(session, version, key)
        if removed is None:
            return
        self._count(f"{version.label}:removed")
        doc_id = document_id(key, version)
        await self.bus.publish(
            TOPIC_CHUNKED_DOCUMENTS,
            doc_id,
            ChunkedDocumentEvent(document_id=doc_id, op="DELETE", removed_chunk_ids=removed),
        )

    # ---- index stage ------------------------------------------------------------------
    async def handle_chunked(self, payload: str) -> None:
        event = ChunkedDocumentEvent.model_validate_json(payload)
        affected = list(dict.fromkeys([*event.chunk_ids, *event.removed_chunk_ids]))
        await self.index_chunks(affected)

    async def index_chunks(self, chunk_ids: list[str]) -> None:
        """Bring OpenSearch in line with Postgres for these chunks."""
        async with self.sessions() as session:
            rows = await chunks_for_index(session, chunk_ids)
        live = {row.id: row for row in rows if row.versions}
        to_delete = [chunk_id for chunk_id in chunk_ids if chunk_id not in live]
        to_embed = [
            row
            for row in live.values()
            if row.indexed_at is None or row.embedding_model != self.embedder.model
        ]
        to_update = [row for row in live.values() if row not in to_embed]

        updated = 0
        if to_update:
            # Chunks Postgres thinks are indexed but OpenSearch lacks (e.g. index rebuilt)
            # come back as missing and are embedded from scratch.
            missing = set(await self.index.update_metadata([self._metadata(r) for r in to_update]))
            to_embed += [row for row in to_update if row.id in missing]
            updated = len(to_update) - len(missing)
        if to_embed:
            vectors = await self.embedder.embed(
                [embedding_input(row.breadcrumb, row.text) for row in to_embed]
            )
            await self.index.upsert(
                [
                    {**self._metadata(row), **self._content(row), "embedding": vector}
                    for row, vector in zip(to_embed, vectors, strict=True)
                ]
            )
            async with self.sessions.begin() as session:
                await mark_indexed(session, [row.id for row in to_embed], self.embedder.model)
        if to_delete:
            await self.index.delete(to_delete)
        self._count("index:embedded", len(to_embed))
        self._count("index:metadata_updated", updated)
        self._count("index:deleted", len(to_delete))

    async def index_pending(self, batch_size: int = 256) -> None:
        """Recovery/backfill: (re)index chunks never indexed or embedded with another model."""
        while True:
            async with self.sessions() as session:
                ids = await chunks_needing_embedding(session, self.embedder.model, batch_size)
            if not ids:
                return
            await self.index_chunks(ids)

    # ---- cleanup ----------------------------------------------------------------------
    async def collect_garbage(self) -> None:
        async with self.sessions.begin() as session:
            orphans = await delete_orphan_chunks(session)
        if orphans:
            await self.index.delete(orphans)
        self._count("chunks:orphans_deleted", len(orphans))

    async def rebuild_anchors(self) -> None:
        for version in self.versions:
            async with self.sessions.begin() as session:
                rows = await rebuild_anchors(session, version.label)
            self._count(f"{version.label}:anchors", rows)

    # ---- helpers ----------------------------------------------------------------------
    def _metadata(self, row: ChunkForIndex) -> dict[str, Any]:
        return {
            "chunk_id": row.id,
            "versions": row.versions,
            "doc_keys": row.doc_keys,
            "titles": row.titles,
            "source_type": "DOC",
            "security_level": "PUBLIC",
        }

    def _content(self, row: ChunkForIndex) -> dict[str, Any]:
        return {
            "text": row.text,
            "breadcrumb": row.breadcrumb,
            "token_count": row.token_count,
            "embedding_model": self.embedder.model,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    def _count(self, key: str, n: int = 1) -> None:
        if n:
            self.stats[key] += n
