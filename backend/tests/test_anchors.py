import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.retrieval.anchors import anchor_pages
from shared.db.models import Chunk, ChunkOccurrence, Document
from shared.identifiers import anchor_terms
from workers.ingest.anchors import rebuild_anchors, score_anchors


def test_anchor_terms_normalizes_identifiers() -> None:
    text = "E11000 from db.collection.insertOne(); try $median, maxTimeMS or BSONObjectTooLarge"
    assert anchor_terms(text) == {
        "e11000",
        "insertone",
        "$median",
        "maxtimems",
        "bsonobjecttoolarge",
    }
    assert anchor_terms("How do I create an index?") == set()


def filler_pages(n: int) -> dict[str, list[str]]:
    return {f"filler/{i}": ["Plain prose without identifiers."] for i in range(n)}


def test_title_defines_and_concentration_scores() -> None:
    chunks = {
        "reference/operator/aggregation/median": ["Use $median in $group.", "More on $median."],
        "release-notes/7.0": ["New: $median.", "Other news.", "More news.", "Even more."],
        **filler_pages(200),
    }
    titles = {page: page for page in chunks} | {
        "reference/operator/aggregation/median": "$median (accumulator)"
    }

    scores = score_anchors(titles, chunks)

    defining = scores["$median", "reference/operator/aggregation/median"]
    mentioning = scores["$median", "release-notes/7.0"]
    assert defining > 2.0 > mentioning > 0


def test_identifiers_on_many_pages_anchor_nothing() -> None:
    chunks = {f"page/{i}": ["Call insertOne() here."] for i in range(10)} | filler_pages(100)
    titles = {page: page for page in chunks}

    assert not any(term == "insertone" for term, _ in score_anchors(titles, chunks))


@pytest.mark.integration
async def test_rebuild_then_lookup(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions.begin() as session:
        for i, (key, title, text) in enumerate(
            [
                ("core/index-unique", "Unique Indexes", "Inserts fail with E11000 duplicate key."),
                ("reference/glossary", "Glossary", "Terms and definitions."),
                *[(f"filler/{n}", f"Page {n}", "Prose.") for n in range(60)],
            ]
        ):
            doc_id = f"9.0:{key}"
            session.add(
                Document(
                    id=doc_id,
                    doc_key=key,
                    version="9.0",
                    url=f"https://x/{key}/",
                    markdown_url=f"https://x/{key}.md",
                    title=title,
                    content_hash="h",
                )
            )
            session.add(Chunk(id=f"{i:016x}", breadcrumb=title, text=text, token_count=5))
            await session.flush()
            session.add(ChunkOccurrence(document_id=doc_id, ordinal=0, chunk_id=f"{i:016x}"))

    async with sessions.begin() as session:
        assert await rebuild_anchors(session, "9.0") > 0
    async with sessions.begin() as session:  # rebuilding replaces, never duplicates
        rows = await rebuild_anchors(session, "9.0")

    assert await anchor_pages(sessions, "9.0", "Why do I get E11000?") == ["core/index-unique"]
    assert await anchor_pages(sessions, "8.0", "Why do I get E11000?") == []
    assert await anchor_pages(sessions, "9.0", "How do I create an index?") == []
    assert rows == 1
