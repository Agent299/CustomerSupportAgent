"""Ingestion end to end: mocked docs site → in-memory bus → real Postgres + OpenSearch."""

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.genai.embeddings import HashEmbedder
from app.retrieval.kb_index import KbIndex
from shared.bus import InMemoryBus
from shared.db.models import Chunk, Document, IngestOutbox
from shared.events import DLQ_SUFFIX, TOPIC_CHUNKED_DOCUMENTS, TOPIC_RAW_DOCUMENTS
from shared.sources import DocVersion
from tests.conftest import TEST_DIMENSIONS
from workers.ingest.crawler import DocsCrawler
from workers.ingest.pipeline import CHUNK_GROUP, INDEX_GROUP, Pipeline

pytestmark = pytest.mark.integration

V9 = DocVersion("9.0", "manual")
V7 = DocVersion("7.0", "v7.0")


def words(text: str) -> int:
    return len(text.split())


def page(title: str, body: str) -> str:
    return (
        "> For the complete MongoDB documentation index, see www.mongodb.com/docs/llms.txt\n\n"
        f"# {title}\n\n## Details\n\n{body}\n"
    )


SHARED = page("Unique Indexes", "E11000 duplicate key error happens on unique index violations.")
ONLY_9 = page("Compound Indexes", "Compound indexes support sort and filter on several fields.")


class FakeDocsSite:
    def __init__(self) -> None:
        self.pages: dict[DocVersion, dict[str, str]] = {
            V9: {"core/unique": SHARED, "core/compound": ONLY_9},
            V7: {"core/unique": SHARED},
        }
        self.failing: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        for version, pages in self.pages.items():
            if url == version.sitemap_url:
                locs = "".join(f"<url><loc>{version.root_url}{k}/</loc></url>" for k in pages)
                return httpx.Response(
                    200,
                    text=f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>',
                )
            for key, markdown in pages.items():
                if url == f"{version.root_url}{key}.md":
                    return httpx.Response(500 if key in self.failing else 200, text=markdown)
        return httpx.Response(404)


class CountingEmbedder(HashEmbedder):
    def __init__(self) -> None:
        super().__init__(TEST_DIMENSIONS)
        self.embedded = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded += len(texts)
        return await super().embed(texts)


@pytest.fixture
async def setup(
    sessions: async_sessionmaker[AsyncSession], kb: KbIndex
) -> AsyncIterator[tuple[Pipeline, FakeDocsSite, CountingEmbedder]]:
    site = FakeDocsSite()
    embedder = CountingEmbedder()
    async with DocsCrawler.make_client(httpx.MockTransport(site.handler)) as client:
        pipeline = Pipeline(
            bus=InMemoryBus(attempts=1),
            sessions=sessions,
            crawler=DocsCrawler(client, rate_per_second=1000, max_retries=0),
            index=kb,
            embedder=embedder,
            versions=(V9, V7),
            count_tokens=words,
        )
        yield pipeline, site, embedder


async def run_once(pipeline: Pipeline) -> None:
    pipeline.stats.clear()
    await pipeline.schedule()
    await pipeline.bus.consume(TOPIC_RAW_DOCUMENTS, CHUNK_GROUP, pipeline.handle_raw)
    await pipeline.bus.consume(TOPIC_CHUNKED_DOCUMENTS, INDEX_GROUP, pipeline.handle_chunked)
    await pipeline.collect_garbage()


async def chunk_count(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(Chunk)) or 0)


async def test_first_run_indexes_every_chunk_with_versions(setup, sessions) -> None:  # type: ignore[no-untyped-def]
    pipeline, _, embedder = setup

    await run_once(pipeline)

    assert await pipeline.index.count() == await chunk_count(sessions) == 2
    assert embedder.embedded == 2  # the shared chunk is embedded once, not per version
    vector = (await embedder.embed(["E11000 duplicate key"]))[0]
    [hit, *_] = await pipeline.index.hybrid_search("E11000 duplicate key", vector)
    assert hit.source["versions"] == ["7.0", "9.0"]
    assert hit.source["doc_keys"] == ["core/unique"]
    assert hit.source["titles"] == ["Unique Indexes"]
    only_7 = await pipeline.index.hybrid_search("compound sort", vector, versions=["7.0"])
    assert [h.source["titles"] for h in only_7] == [["Unique Indexes"]]


async def test_rerun_is_a_noop_with_no_embedding_calls(setup, sessions) -> None:  # type: ignore[no-untyped-def]
    pipeline, _, embedder = setup
    await run_once(pipeline)
    embedder.embedded = 0
    chunked_events = len(pipeline.bus.topics[TOPIC_CHUNKED_DOCUMENTS])  # type: ignore[attr-defined]

    await run_once(pipeline)

    assert embedder.embedded == 0
    assert pipeline.stats["9.0:unchanged"] == 2 and pipeline.stats["7.0:unchanged"] == 1
    # Unchanged pages emit nothing downstream.
    assert len(pipeline.bus.topics[TOPIC_CHUNKED_DOCUMENTS]) == chunked_events  # type: ignore[attr-defined]


async def test_existing_chunk_in_new_version_updates_metadata_without_embedding(setup) -> None:  # type: ignore[no-untyped-def]
    pipeline, site, embedder = setup
    await run_once(pipeline)
    embedder.embedded = 0
    site.pages[V7]["core/compound"] = ONLY_9  # same text appears in 7.0 now

    await run_once(pipeline)

    assert embedder.embedded == 0
    assert pipeline.stats["index:metadata_updated"] >= 1
    docs = await pipeline.index.hybrid_search(
        "compound", (await embedder.embed(["compound"]))[0], versions=["7.0"]
    )
    assert any(d.source["titles"] == ["Compound Indexes"] for d in docs)


async def test_changed_page_replaces_old_chunk_in_index(setup, sessions) -> None:  # type: ignore[no-untyped-def]
    pipeline, site, embedder = setup
    await run_once(pipeline)
    site.pages[V9]["core/compound"] = page("Compound Indexes", "Rewritten: prefix rules apply.")

    await run_once(pipeline)

    assert pipeline.stats["9.0:updated"] == 1
    assert pipeline.stats["chunks:orphans_deleted"] == 1
    assert await pipeline.index.count() == await chunk_count(sessions) == 2
    hits = await pipeline.index.hybrid_search("prefix rules", (await embedder.embed(["x"]))[0])
    assert any("prefix rules" in h.source["text"] for h in hits)
    assert not any("several fields" in h.source["text"] for h in hits)


async def test_removed_page_drops_version_then_chunk(setup, sessions) -> None:  # type: ignore[no-untyped-def]
    pipeline, site, embedder = setup
    await run_once(pipeline)
    del site.pages[V9]["core/unique"]

    await run_once(pipeline)

    async with sessions() as session:
        doc = await session.get(Document, "9.0:core/unique")
    assert doc is not None and doc.status == "deleted"
    hits = await pipeline.index.hybrid_search("E11000", (await embedder.embed(["E11000"]))[0])
    unique = [h for h in hits if h.source["titles"] == ["Unique Indexes"]]
    assert [h.source["versions"] for h in unique] == [["7.0"]]  # 7.0 still has it

    del site.pages[V7]["core/unique"]
    await run_once(pipeline)
    assert await pipeline.index.count() == await chunk_count(sessions) == 1


async def test_missing_index_documents_are_re_embedded(setup, sessions) -> None:  # type: ignore[no-untyped-def]
    pipeline, _, embedder = setup
    await run_once(pipeline)
    async with sessions() as session:
        ids = list(await session.scalars(select(Chunk.id)))
    await pipeline.index.delete(ids)  # e.g. the search cluster was rebuilt
    embedder.embedded = 0

    await pipeline.index_chunks(ids)

    assert embedder.embedded == 2
    assert await pipeline.index.count() == 2


async def test_webhook_outbox_rows_are_published(setup, sessions) -> None:  # type: ignore[no-untyped-def]
    pipeline, _, _ = setup
    async with sessions.begin() as session:
        session.add(
            IngestOutbox(payload={"version": "9.0", "key": "core/compound", "op": "UPSERT"})
        )

    await pipeline.drain_outbox()

    async with sessions() as session:
        row = await session.scalar(select(IngestOutbox))
    assert row is not None and row.published_at is not None
    [(key, _)] = pipeline.bus.topics[TOPIC_RAW_DOCUMENTS]  # type: ignore[attr-defined]
    assert key == "9.0:core/compound"


async def test_page_that_keeps_failing_is_dead_lettered(setup) -> None:  # type: ignore[no-untyped-def]
    pipeline, site, _ = setup
    site.failing.add("core/compound")

    await run_once(pipeline)

    dlq = pipeline.bus.topics[TOPIC_RAW_DOCUMENTS + DLQ_SUFFIX]  # type: ignore[attr-defined]
    assert [key for key, _ in dlq] == ["9.0:core/compound"]
    assert pipeline.stats["9.0:added"] == 1 and pipeline.stats["7.0:added"] == 1
