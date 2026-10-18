"""Query-time retrieval (docs/PLAN.md §8.6 T4 + T6).

Hybrid search returns chunks; several chunks of one long page often fill the top ranks
(docs/evals/retrieval-baseline-v0.md). ``diversify`` caps chunks per page so the context
covers more pages, then packs passages in rank order up to a token budget. Chunk text comes
from the index ``_source``, which already holds it: no extra Postgres round trip.

``search_context`` adds the two retrieval aids chosen from
docs/research/graphrag-retrieval-study.md (option B): expansion terms in the lexical query,
and the best chunks of identifier-anchored pages, fused by reciprocal rank.
"""

import asyncio
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from app.retrieval.kb_index import Hit, KbIndex
from shared.sources import get_version, page_url

SEARCH_K = 40
ANCHOR_SEARCH_K = 60
MAX_CHUNKS_PER_PAGE = 2
CONTEXT_TOKEN_BUDGET = 6000
RRF_K = 60


@dataclass(frozen=True)
class Passage:
    chunk_id: str
    score: float
    text: str
    breadcrumb: str
    title: str
    doc_keys: list[str]
    versions: list[str]
    url: str
    token_count: int

    @property
    def page(self) -> str:
        return self.doc_keys[0] if self.doc_keys else self.chunk_id


def to_passage(hit: Hit, version: str) -> Passage:
    source = hit.source
    doc_keys = list(source.get("doc_keys", []))
    titles = list(source.get("titles", []))
    return Passage(
        chunk_id=hit.chunk_id,
        score=hit.score,
        text=str(source.get("text", "")),
        breadcrumb=str(source.get("breadcrumb", "")),
        title=titles[0] if titles else "",
        doc_keys=doc_keys,
        versions=list(source.get("versions", [])),
        url=page_url(doc_keys[0], get_version(version)) if doc_keys else "",
        token_count=int(source.get("token_count", 0)),
    )


def diversify(
    passages: list[Passage],
    *,
    max_per_page: int = MAX_CHUNKS_PER_PAGE,
    token_budget: int = CONTEXT_TOKEN_BUDGET,
) -> list[Passage]:
    """Keep rank order, at most ``max_per_page`` chunks per page, within ``token_budget``.

    The top passage is always kept, even if it alone exceeds the budget."""
    kept: list[Passage] = []
    per_page: dict[str, int] = {}
    tokens = 0
    for passage in passages:
        if per_page.get(passage.page, 0) >= max_per_page:
            continue
        if kept and tokens + passage.token_count > token_budget:
            continue  # a smaller passage further down may still fit
        kept.append(passage)
        per_page[passage.page] = per_page.get(passage.page, 0) + 1
        tokens += passage.token_count
    return kept


def rrf(lists: Iterable[list[Passage]]) -> list[Passage]:
    """Reciprocal-rank fusion: a passage high in any list ranks high overall."""
    scores: dict[str, float] = defaultdict(float)
    by_id: dict[str, Passage] = {}
    for passages in lists:
        for rank, passage in enumerate(passages, start=1):
            scores[passage.chunk_id] += 1 / (RRF_K + rank)
            by_id.setdefault(passage.chunk_id, passage)
    return [by_id[i] for i in sorted(scores, key=lambda i: -scores[i])]


def best_per_page(passages: list[Passage], pages: list[str], per_page: int = 2) -> list[Passage]:
    """The top ``per_page`` passages of each page, pages in the given order."""
    best: dict[str, list[Passage]] = defaultdict(list)
    for passage in passages:
        page = next((k for k in passage.doc_keys if k in pages), None)
        if page is not None and len(best[page]) < per_page:
            best[page].append(passage)
    return [p for page in pages for p in best[page]]


async def search_context(
    index: KbIndex,
    *,
    text: str,
    vector: list[float],
    version: str,
    anchors: list[str],
) -> list[Passage]:
    """Hybrid search on ``text`` (BM25) + ``vector`` (kNN), fused with the anchored pages'
    best chunks, then packed for the model."""
    searches = [index.hybrid_search(text, vector, versions=[version], k=SEARCH_K)]
    if anchors:
        searches.append(
            index.hybrid_search(
                text, vector, versions=[version], k=ANCHOR_SEARCH_K, doc_keys=anchors
            )
        )
    results = await asyncio.gather(*searches)
    lists = [[to_passage(hit, version) for hit in hits] for hits in results]
    if anchors:
        lists[1] = best_per_page(lists[1], anchors)
    return diversify(rrf(lists) if len(lists) > 1 else lists[0])
