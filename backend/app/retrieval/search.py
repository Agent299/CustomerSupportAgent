"""Query-time retrieval (docs/PLAN.md §8.6 T4 + T6).

Hybrid search returns chunks; several chunks of one long page often fill the top ranks
(docs/evals/retrieval-baseline-v0.md). ``diversify`` caps chunks per page so the context
covers more pages, then packs passages in rank order up to a token budget. Chunk text comes
from the index ``_source``, which already holds it: no extra Postgres round trip.
"""

from dataclasses import dataclass

from app.genai.embeddings import Embedder
from app.retrieval.kb_index import Hit, KbIndex
from shared.sources import get_version, page_url

SEARCH_K = 40
MAX_CHUNKS_PER_PAGE = 2
CONTEXT_TOKEN_BUDGET = 6000


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


async def retrieve(
    index: KbIndex,
    embedder: Embedder,
    query: str,
    version: str,
    *,
    k: int = SEARCH_K,
    max_per_page: int = MAX_CHUNKS_PER_PAGE,
    token_budget: int = CONTEXT_TOKEN_BUDGET,
) -> list[Passage]:
    [vector] = await embedder.embed([query])
    hits = await index.hybrid_search(query, vector, versions=[version], k=k)
    passages = [to_passage(hit, version) for hit in hits]
    return diversify(passages, max_per_page=max_per_page, token_budget=token_budget)
