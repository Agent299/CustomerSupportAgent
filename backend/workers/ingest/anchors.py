"""Identifier anchor index: identifier → the pages that define it or use it most.

Built from Postgres after each ingestion run, one version at a time, and replaced wholesale
(it takes seconds). Scoring follows docs/research/graphrag-retrieval-study.md:

- a page whose *title* names the identifier defines it (+2.0);
- the 5 pages that mention it most get idf/10 * (0.5 + share of the page's chunks);
- identifiers on more than 2 % of pages ("MongoDB", "insertOne" in every example) anchor
  nothing and are left out.
"""

import math
from collections import Counter, defaultdict

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Chunk, ChunkOccurrence, Document, IdentifierAnchor
from shared.identifiers import anchor_terms

MAX_PAGE_SHARE = 0.02
TOP_PAGES = 5
DEFINES_SCORE = 2.0
MAX_TERM_LENGTH = 128  # the column width


def score_anchors(
    titles: dict[str, str], chunk_texts: dict[str, list[str]]
) -> dict[tuple[str, str], float]:
    """(identifier, doc_key) → score, for one version's pages."""
    n_pages = len(titles)
    mentions: dict[str, Counter[str]] = defaultdict(Counter)
    for page, texts in chunk_texts.items():
        for text in texts:
            for term in anchor_terms(text):
                mentions[term][page] += 1
    defines: dict[str, set[str]] = defaultdict(set)
    for page, title in titles.items():
        for term in anchor_terms(title):
            defines[term].add(page)

    scores: dict[tuple[str, str], float] = defaultdict(float)
    for term in set(mentions) | set(defines):
        if len(term) > MAX_TERM_LENGTH:
            continue
        pages = mentions.get(term, Counter())
        if len(pages) > MAX_PAGE_SHARE * n_pages:
            continue
        for page in defines.get(term, ()):
            scores[term, page] += DEFINES_SCORE
        if not pages:
            continue
        idf = math.log(n_pages / len(pages))
        for page, count in pages.most_common(TOP_PAGES):
            share = count / len(chunk_texts[page])
            scores[term, page] += idf / 10 * (0.5 + share)
    return dict(scores)


async def rebuild_anchors(session: AsyncSession, version: str) -> int:
    """Replace the version's anchors; returns the number of rows. Caller commits."""
    rows = await session.execute(
        select(Document.doc_key, Document.title, Chunk.text)
        .join(ChunkOccurrence, ChunkOccurrence.document_id == Document.id)
        .join(Chunk, Chunk.id == ChunkOccurrence.chunk_id)
        .where(Document.version == version, Document.status == "active")
    )
    titles: dict[str, str] = {}
    chunk_texts: dict[str, list[str]] = defaultdict(list)
    for doc_key, title, text in rows:
        titles[doc_key] = title
        chunk_texts[doc_key].append(text)
    scores = score_anchors(titles, chunk_texts)
    await session.execute(delete(IdentifierAnchor).where(IdentifierAnchor.version == version))
    session.add_all(
        IdentifierAnchor(version=version, identifier=term, doc_key=page, score=score)
        for (term, page), score in scores.items()
    )
    return len(scores)
