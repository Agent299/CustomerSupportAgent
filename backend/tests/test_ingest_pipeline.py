"""End-to-end ingestion against real Postgres with a mocked docs site."""

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.db.models import Chunk, ChunkOccurrence, Document, IngestRun
from shared.sources import DocVersion
from workers.ingest.crawler import DocsCrawler
from workers.ingest.run import run

pytestmark = pytest.mark.integration

V9 = DocVersion("9.0", "manual")
V7 = DocVersion("7.0", "v7.0")
ROBOTS = "User-agent: *\nDisallow: /docs/meta/\n"


def words(text: str) -> int:
    return len(text.split())


def sitemap(version: DocVersion, keys: list[str]) -> str:
    urls = "".join(f"<url><loc>{version.root_url}{key}/</loc></url>" for key in keys)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'


def page(title: str, body: str, version: DocVersion) -> str:
    return (
        "> For the complete MongoDB documentation index, see www.mongodb.com/docs/llms.txt\n\n"
        f"# {title}\n\nIntro for {title} with enough words to stand alone as a chunk here.\n\n"
        f"## Details\n\n{body} See [doc](https://www.mongodb.com/docs/{version.path}/core/document.md#x).\n"
    )


class FakeDocsSite:
    """Mutable in-memory docs site served through httpx.MockTransport."""

    def __init__(self) -> None:
        self.pages: dict[DocVersion, dict[str, str]] = {
            V9: {
                "core/a": page("A", "Shared body text for page A across versions.", V9),
                "core/b": page("B", "Body of B only in 9.0.", V9),
            },
            V7: {"core/a": page("A", "Shared body text for page A across versions.", V7)},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS)
        for version, pages in self.pages.items():
            if url == version.sitemap_url:
                return httpx.Response(200, text=sitemap(version, sorted(pages)))
            for key, markdown in pages.items():
                if url == f"{version.root_url}{key}.md":
                    return httpx.Response(200, text=markdown)
        return httpx.Response(404)


async def ingest(site: FakeDocsSite, database_url: str, versions=(V9, V7)):  # type: ignore[no-untyped-def]
    async with DocsCrawler.make_client(httpx.MockTransport(site.handler)) as client:
        crawler = DocsCrawler(client, rate_per_second=1000)
        return await run(versions, database_url=database_url, crawler=crawler, count_tokens=words)


async def count(sessions: async_sessionmaker[AsyncSession], model: type) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_first_run_stores_pages_and_dedupes_across_versions(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    stats = await ingest(FakeDocsSite(), database_url)

    assert stats.pages == {"9.0:added": 2, "7.0:added": 1}
    assert await count(sessions, Document) == 3
    # Each page is one chunk (its short intro folds into "Details").
    # Page A is identical in 9.0 and 7.0 → stored once; B adds one more.
    assert await count(sessions, Chunk) == 2
    assert await count(sessions, ChunkOccurrence) == 3
    async with sessions() as session:
        run_row = await session.scalar(select(IngestRun))
        doc = await session.get(Document, "7.0:core/a")
    assert run_row is not None and run_row.status == "success"
    assert doc is not None and doc.url == "https://www.mongodb.com/docs/v7.0/core/a/"


async def test_rerun_without_changes_is_a_noop(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    site = FakeDocsSite()
    await ingest(site, database_url)

    stats = await ingest(site, database_url)

    assert stats.pages == {"9.0:unchanged": 2, "7.0:unchanged": 1}
    assert stats.new_chunk_ids == set()
    assert await count(sessions, Chunk) == 2


async def test_changed_page_replaces_its_chunks_and_orphans_are_removed(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    site = FakeDocsSite()
    await ingest(site, database_url)
    site.pages[V9]["core/b"] = page("B", "Rewritten body of B.", V9)

    stats = await ingest(site, database_url)

    assert stats.pages["9.0:updated"] == 1
    assert len(stats.new_chunk_ids) == 1
    assert stats.orphan_chunks_deleted == 1  # B's old chunk
    assert await count(sessions, Chunk) == 2


async def test_page_removed_from_sitemap_is_marked_deleted(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    site = FakeDocsSite()
    await ingest(site, database_url)
    del site.pages[V9]["core/b"]

    stats = await ingest(site, database_url)

    assert stats.pages["9.0:removed"] == 1
    async with sessions() as session:
        doc = await session.get(Document, "9.0:core/b")
    assert doc is not None and doc.status == "deleted"
    assert stats.orphan_chunks_deleted == 1
    assert await count(sessions, Chunk) == 1  # page A's shared chunk survives


async def test_secrets_are_redacted_before_storage(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    site = FakeDocsSite()
    site.pages[V9]["core/b"] = page(
        "B", "Use mongodb://admin:hunter2@db.example.com to connect.", V9
    )

    await ingest(site, database_url, versions=(V9,))

    async with sessions() as session:
        texts = list(await session.scalars(select(Chunk.text)))
    assert not any("hunter2" in text for text in texts)
    assert any("<PASSWORD_MASKED>" in text for text in texts)
