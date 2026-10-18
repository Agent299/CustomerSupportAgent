"""GraphRAG retrieval study: does graph expansion beat hybrid search on golden set v0?

Research code, not production. Every method produces a ranked passage list that goes through
the same packing as the live pipeline (``diversify``: <= 2 chunks per page, 6k tokens) and is
scored with the same metrics:

- R@k / MRR@10: rank of the first passage from an expected page in the packed list
- context recall: an expected page is in the packed context
- fact recall: share of the example's key facts (verbatim doc quotes) present in the packed
  context text, i.e. whether the model could have stated them

Methods
- base: hybrid BM25 + kNN (weights 0.3/0.7), top 40 (the live pipeline)
- w=<b>: hybrid with BM25 weight b (non-graph alternative)
- llm-rerank: cheapest model reorders the top 40 (non-graph alternative)
- llm-expand: cheapest model adds likely doc terms to the query (non-graph alternative)
- entity: pages that define / most mention identifiers in the query (error names, $ops, APIs)
- link1: pages linked to/from the top 10 pages, hub-damped
- ppr: personalized PageRank over the page link graph, seeded by the top 10 pages
- entity+ppr: both seeds
- +siblings: neighbouring chunks (ordinal +/- 1) of the top 3 passages

Graph-ranked pages enter the list via reciprocal-rank fusion with the hybrid list; each page
is represented by its best chunks for the query (hybrid search filtered to those pages).

    cd backend && uv run python -m research.graphrag_study \\
        --out ../docs/research/graphrag-study-run1.json
"""

import argparse
import asyncio
import json
import math
import re
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opensearchpy import AsyncOpenSearch
from sqlalchemy import text

from app.config import get_settings
from app.genai.embeddings import make_embedder
from app.genai.llm import LlmClient, TaskConfig
from app.retrieval import kb_index
from app.retrieval.kb_index import Hit
from app.retrieval.search import Passage, diversify, to_passage
from evals.golden import GoldenExample, load_golden
from evals.validate import normalize
from shared.db.session import make_engine, make_session_factory

K = 40
SEEDS = 10
RRF_K = 60
MAX_ENTITY_PAGE_SHARE = 0.02
KS = (1, 3, 5, 10)

# ---------------------------------------------------------------- identifiers / entities
_ENTITY = re.compile(
    r"\$[a-zA-Z]\w+"  # $median, $group
    r"|\b[a-z]+(?:\.[a-zA-Z]+)*\.[a-z]+[A-Za-z]*\(\)"  # db.collection.insertOne(), cursor.sort()
    r"|\b[a-z]+[A-Z][A-Za-z0-9]*\b"  # camelCase: maxTimeMS, noCursorTimeout, insertOne
    r"|\b[A-Z][A-Za-z0-9]*[a-z][A-Z][A-Za-z0-9]*\b"  # PascalCase: DuplicateKey, CursorNotFound
    r"|\b[A-Z]{2,}[a-z]\w*[A-Z]\w*\b"  # BSONObjectTooLarge
    r"|\bE\d{4,5}\b"  # E11000
)


def entities(value: str) -> set[str]:
    found = set()
    for match in _ENTITY.findall(value):
        token = match.rstrip("()").lower()
        token = token.rsplit(".", 1)[-1] if "." in token else token  # cursor.sort -> sort
        if len(token) >= 4:
            found.add(token)
    return found


# ---------------------------------------------------------------- graph
@dataclass
class Graph:
    version: str
    titles: dict[str, str] = field(default_factory=dict)
    page_chunks: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # (id, text)
    links: dict[str, Counter[str]] = field(default_factory=dict)  # page -> target counts
    undirected: dict[str, dict[str, float]] = field(default_factory=dict)
    in_degree: Counter[str] = field(default_factory=Counter)
    mentions: dict[str, Counter[str]] = field(default_factory=dict)  # entity -> page counts
    defines: dict[str, set[str]] = field(default_factory=dict)  # entity -> pages (title)

    def hub_weight(self, page: str) -> float:
        """Damp pages everything links to (glossary, install guides)."""
        return 1.0 / math.log(2 + self.in_degree[page])


async def load_graph(sessions: Any, version: str) -> Graph:
    graph = Graph(version)
    async with sessions() as session:
        rows = await session.execute(
            text(
                "select d.doc_key, d.title, o.ordinal, c.id, c.text, c.links "
                "from documents d join chunk_occurrences o on o.document_id = d.id "
                "join chunks c on c.id = o.chunk_id "
                "where d.version = :v and d.status = 'active' order by d.doc_key, o.ordinal"
            ),
            {"v": version},
        )
        for doc_key, title, _ordinal, chunk_id, body, links in rows:
            graph.titles[doc_key] = title
            graph.page_chunks.setdefault(doc_key, []).append((chunk_id, body))
            for link in links or []:
                target = link.get("key")
                if target and target != doc_key:
                    graph.links.setdefault(doc_key, Counter())[target] += 1
            for entity in entities(body):
                graph.mentions.setdefault(entity, Counter())[doc_key] += 1
    for targets in graph.links.values():
        for target in list(targets):
            if target not in graph.titles:
                del targets[target]
                continue
            graph.in_degree[target] += 1
    for page, targets in graph.links.items():
        for target, count in targets.items():
            weight = math.log1p(count)
            graph.undirected.setdefault(page, {})
            graph.undirected.setdefault(target, {})
            graph.undirected[page][target] = graph.undirected[page].get(target, 0) + weight
            graph.undirected[target][page] = graph.undirected[target].get(page, 0) + weight
    for page, title in graph.titles.items():
        for entity in entities(title):
            graph.defines.setdefault(entity, set()).add(page)
    return graph


def entity_pages(graph: Graph, query: str, limit: int = 5) -> dict[str, float]:
    """Pages whose title defines a query identifier, else where it is most concentrated."""
    scores: dict[str, float] = defaultdict(float)
    n_pages = len(graph.titles)
    for entity in entities(query):
        mentioned_on = len(graph.mentions.get(entity, ()))
        if mentioned_on > MAX_ENTITY_PAGE_SHARE * n_pages:
            continue  # generic ("MongoDB"): anchors nothing
        for page in graph.defines.get(entity, set()):
            scores[page] += 2.0
        mentions = graph.mentions.get(entity, Counter())
        if not mentions:
            continue
        idf = math.log(n_pages / len(mentions))
        for page, count in mentions.most_common(limit):
            share = count / len(graph.page_chunks[page])  # how central the entity is to the page
            scores[page] += idf / 10 * (0.5 + share)
    return dict(scores)


def link_neighbours(graph: Graph, seeds: dict[str, float]) -> dict[str, float]:
    scores: dict[str, float] = defaultdict(float)
    for seed, weight in seeds.items():
        for neighbour, edge in graph.undirected.get(seed, {}).items():
            scores[neighbour] += weight * edge * graph.hub_weight(neighbour)
    for seed, weight in seeds.items():  # seeds that other seeds point at rise too
        scores[seed] += weight
    return dict(scores)


def personalized_pagerank(
    graph: Graph, seeds: dict[str, float], alpha: float = 0.25, iterations: int = 25
) -> dict[str, float]:
    total = sum(seeds.values()) or 1.0
    restart = {page: weight / total for page, weight in seeds.items()}
    out_weight = {page: sum(edges.values()) for page, edges in graph.undirected.items()}
    rank = dict(restart)
    for _ in range(iterations):
        nxt: dict[str, float] = defaultdict(float)
        for page, value in rank.items():
            edges = graph.undirected.get(page)
            if not edges:
                nxt[page] += (1 - alpha) * value
                continue
            for target, weight in edges.items():
                nxt[target] += (1 - alpha) * value * weight / out_weight[page]
        for page, value in restart.items():
            nxt[page] += alpha * value
        rank = nxt
    return {page: value * graph.hub_weight(page) for page, value in rank.items()}


# ---------------------------------------------------------------- search helpers
class Searcher:
    def __init__(self, client: AsyncOpenSearch, alias: str) -> None:
        self.client = client
        self.alias = alias

    async def ensure_pipeline(self, bm25: float) -> str:
        name = f"study-hybrid-{int(bm25 * 100)}"
        await self.client.search_pipeline.put(
            id=name,
            body={
                "phase_results_processors": [
                    {
                        "normalization-processor": {
                            "normalization": {"technique": "min_max"},
                            "combination": {
                                "technique": "arithmetic_mean",
                                "parameters": {"weights": [bm25, round(1 - bm25, 2)]},
                            },
                        }
                    }
                ]
            },
        )
        return name

    async def hybrid(
        self,
        query: str,
        vector: list[float],
        version: str,
        *,
        k: int = K,
        pages: list[str] | None = None,
        pipeline: str = kb_index.HYBRID_PIPELINE,
    ) -> list[Hit]:
        filters: list[dict[str, Any]] = [
            {"term": {"security_level": "PUBLIC"}},
            {"terms": {"versions": [version]}},
        ]
        if pages:
            filters.append({"terms": {"doc_keys": pages}})
        body = {
            "size": k,
            "_source": {"excludes": ["embedding"]},
            "query": {
                "hybrid": {
                    "queries": [
                        {
                            "bool": {
                                "must": {
                                    "multi_match": {
                                        "query": query,
                                        "fields": ["text", "breadcrumb^2", "titles^2"],
                                    }
                                },
                                "filter": filters,
                            }
                        },
                        {
                            "knn": {
                                "embedding": {
                                    "vector": vector,
                                    "k": k,
                                    "filter": {"bool": {"filter": filters}},
                                }
                            }
                        },
                    ]
                }
            },
        }
        response = await self.client.search(
            index=self.alias, body=body, params={"search_pipeline": pipeline}
        )
        return [
            Hit(chunk_id=h["_id"], score=float(h["_score"]), source=h["_source"])
            for h in response["hits"]["hits"]
        ]


def page_scores_from(passages: list[Passage], limit: int = SEEDS) -> dict[str, float]:
    """Seed weights for the top pages: 1/rank of each page's best passage."""
    seeds: dict[str, float] = {}
    for rank, passage in enumerate(passages, start=1):
        if passage.page not in seeds:
            seeds[passage.page] = 1.0 / rank
        if len(seeds) >= limit:
            break
    return seeds


def rrf(lists: Iterable[tuple[list[Passage], float]]) -> list[Passage]:
    scores: dict[str, float] = defaultdict(float)
    by_id: dict[str, Passage] = {}
    for passages, weight in lists:
        for rank, passage in enumerate(passages, start=1):
            scores[passage.chunk_id] += weight / (RRF_K + rank)
            by_id.setdefault(passage.chunk_id, passage)
    return [by_id[i] for i in sorted(scores, key=lambda i: -scores[i])]


async def graph_list(
    searcher: Searcher,
    example: GoldenExample,
    vector: list[float],
    page_scores: dict[str, float],
    *,
    exclude: set[str],
    top_pages: int = 8,
) -> list[Passage]:
    """Best chunks (<= 2) of the top graph-ranked pages, in graph rank order."""
    ranked = [p for p in sorted(page_scores, key=lambda p: -page_scores[p]) if p not in exclude]
    ranked = ranked[:top_pages]
    if not ranked:
        return []
    hits = await searcher.hybrid(
        example.search_text, vector, example.effective_version, k=60, pages=ranked
    )
    best: dict[str, list[Passage]] = defaultdict(list)
    for hit in hits:
        passage = to_passage(hit, example.effective_version)
        for page in passage.doc_keys:
            if page in ranked and len(best[page]) < 2:
                best[page].append(passage)
                break
    return [p for page in ranked for p in best.get(page, [])]


def with_siblings(graph: Graph, passages: list[Passage], top: int = 3) -> list[Passage]:
    """Insert the chunks around the top passages right after them (same page)."""
    out: list[Passage] = []
    seen: set[str] = set()
    for index, passage in enumerate(passages):
        if passage.chunk_id in seen:
            continue
        out.append(passage)
        seen.add(passage.chunk_id)
        if index >= top:
            continue
        chunks = graph.page_chunks.get(passage.page, [])
        ids = [c for c, _ in chunks]
        if passage.chunk_id not in ids:
            continue
        position = ids.index(passage.chunk_id)
        for neighbour in (position + 1, position - 1):
            if 0 <= neighbour < len(chunks):
                chunk_id, body = chunks[neighbour]
                if chunk_id in seen:
                    continue
                seen.add(chunk_id)
                out.append(
                    Passage(
                        chunk_id=chunk_id,
                        score=0.0,
                        text=body,
                        breadcrumb=passage.breadcrumb,
                        title=passage.title,
                        doc_keys=[passage.page],
                        versions=passage.versions,
                        url=passage.url,
                        token_count=max(1, len(body) // 4),
                    )
                )
    return out


# ---------------------------------------------------------------- LLM alternatives
RERANK_SCHEMA = {
    "type": "object",
    "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["ids"],
    "additionalProperties": False,
}
EXPAND_SCHEMA = {
    "type": "object",
    "properties": {"terms": {"type": "string"}},
    "required": ["terms"],
    "additionalProperties": False,
}


async def llm_rerank(llm: LlmClient, query: str, passages: list[Passage]) -> list[Passage]:
    numbered = "\n".join(
        f"[{i}] {p.title} > {p.breadcrumb}: {p.text[:350]!r}" for i, p in enumerate(passages)
    )
    raw = await llm.complete(
        "rewrite",
        [
            {
                "role": "system",
                "content": "Rank MongoDB documentation passages by how directly they answer "
                "the question. Return the bracket numbers of the 15 most useful passages, "
                'best first, as strings in "ids".',
            },
            {"role": "user", "content": f"Question: {query}\n\nPassages:\n{numbered}"},
        ],
        json_schema=RERANK_SCHEMA,
    )
    order: list[int] = []
    for item in json.loads(raw)["ids"]:
        digits = re.sub(r"\D", "", str(item))
        if digits and int(digits) < len(passages) and int(digits) not in order:
            order.append(int(digits))
    rest = [i for i in range(len(passages)) if i not in order]
    return [passages[i] for i in order + rest]


async def llm_expand(llm: LlmClient, query: str) -> str:
    raw = await llm.complete(
        "rewrite",
        [
            {
                "role": "system",
                "content": "For a MongoDB support question, name the MongoDB concepts, "
                "features, commands or error names the answer is documented under, as a short "
                'space-separated list of search terms in "terms". Do not answer.',
            },
            {"role": "user", "content": query},
        ],
        json_schema=EXPAND_SCHEMA,
    )
    return str(json.loads(raw)["terms"])


# ---------------------------------------------------------------- metrics
def score(example: GoldenExample, packed: list[Passage]) -> dict[str, Any]:
    expected = set(example.expected_sources)
    rank = next(
        (i for i, p in enumerate(packed, start=1) if expected & set(p.doc_keys)),
        None,
    )
    context = normalize("\n".join(p.text for p in packed))
    facts = [normalize(f) in context for f in example.key_facts]
    return {
        "rank": rank,
        "context_hit": rank is not None,
        "fact_recall": sum(facts) / len(facts),
        "all_facts": all(facts),
        "pages": len({p.page for p in packed}),
        "tokens": sum(p.token_count for p in packed),
        "top_pages": list(dict.fromkeys(p.page for p in packed))[:6],
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, float]:
    n = len(rows)
    out = {f"R@{k}": sum(1 for r in rows if r["rank"] and r["rank"] <= k) / n for k in KS}
    out["MRR@10"] = sum(1 / r["rank"] for r in rows if r["rank"] and r["rank"] <= 10) / n
    out["context_recall"] = sum(r["context_hit"] for r in rows) / n
    out["fact_recall"] = sum(r["fact_recall"] for r in rows) / n
    out["all_facts"] = sum(r["all_facts"] for r in rows) / n
    out["pages"] = sum(r["pages"] for r in rows) / n
    out["n"] = float(n)
    return out


# ---------------------------------------------------------------- driver
async def run(out: Path, use_llm: bool) -> None:
    settings = get_settings()
    golden = [e for e in load_golden().examples if e.expected_route == "ANSWERED"]
    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)
    graphs = {
        v: await load_graph(sessions, v) for v in sorted({e.effective_version for e in golden})
    }
    client = kb_index.make_client(settings)
    searcher = Searcher(client, settings.opensearch_index)
    embedder = make_embedder(settings)
    vectors = await embedder.embed([e.search_text for e in golden])
    llm = None
    if use_llm and settings.openai_api_key is not None and settings.model_easy:
        from openai import AsyncOpenAI

        llm = LlmClient(
            AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value(), max_retries=2),
            {"rewrite": TaskConfig(settings.model_easy, 2000, settings.model_easy_reasoning)},
        )
    pipelines = {b: await searcher.ensure_pipeline(b) for b in (0.1, 0.5, 0.7)}

    results: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    timings: dict[str, list[float]] = defaultdict(list)
    for example, vector in zip(golden, vectors, strict=True):
        version = example.effective_version
        graph = graphs[version]
        hits = await searcher.hybrid(example.search_text, vector, version)
        base = [to_passage(h, version) for h in hits]
        seeds = page_scores_from(base)
        exclude: set[str] = set()  # graph lists may repeat base pages; RRF merges them
        methods: dict[str, list[Passage]] = {"base": base}
        methods["base (no packing)"] = base  # chunk ranks as in the retrieval baseline

        for b, pipeline in pipelines.items():
            weighted = await searcher.hybrid(
                example.search_text, vector, version, pipeline=pipeline
            )
            methods[f"w={b}"] = [to_passage(h, version) for h in weighted]

        ent = entity_pages(graph, example.search_text)
        link = link_neighbours(graph, seeds)
        ppr = personalized_pagerank(graph, seeds)
        ent_seeds = dict(seeds)
        for page, value in ent.items():
            ent_seeds[page] = ent_seeds.get(page, 0) + value / 4
        ppr_ent = personalized_pagerank(graph, ent_seeds)

        g_ent = await graph_list(searcher, example, vector, ent, exclude=exclude)
        g_link = await graph_list(searcher, example, vector, link, exclude=exclude)
        g_ppr = await graph_list(searcher, example, vector, ppr, exclude=exclude)
        g_ppr_ent = await graph_list(searcher, example, vector, ppr_ent, exclude=exclude)
        methods["entity"] = rrf([(base, 1.0), (g_ent, 1.0)])
        methods["link1"] = rrf([(base, 1.0), (g_link, 0.7)])
        methods["ppr"] = rrf([(base, 1.0), (g_ppr, 0.7)])
        methods["entity+ppr"] = rrf([(base, 1.0), (g_ppr_ent, 0.7), (g_ent, 1.0)])
        methods["entity+ppr+siblings"] = with_siblings(graph, methods["entity+ppr"])
        methods["base+siblings"] = with_siblings(graph, base)

        if llm is not None:
            started = time.monotonic()
            terms = await llm_expand(llm, example.search_text)
            timings["expand_ms"].append((time.monotonic() - started) * 1000)
            expanded_query = f"{example.search_text}\n{terms}"
            [expanded_vector] = await embedder.embed([expanded_query])
            exp_hits = await searcher.hybrid(expanded_query, expanded_vector, version)
            methods["llm-expand"] = [to_passage(h, version) for h in exp_hits]
            started = time.monotonic()
            methods["llm-rerank"] = await llm_rerank(llm, example.search_text, base)
            timings["rerank_ms"].append((time.monotonic() - started) * 1000)
            methods["llm-expand+entity"] = rrf([(methods["llm-expand"], 1.0), (g_ent, 1.0)])
            methods["llm-expand+rerank"] = await llm_rerank(
                llm, example.search_text, methods["llm-expand"]
            )
            methods["llm-expand+entity+ppr"] = rrf(
                [(methods["llm-expand"], 1.0), (g_ppr_ent, 0.7), (g_ent, 1.0)]
            )

        for name, passages in methods.items():
            packed = passages[:10] if name == "base (no packing)" else diversify(passages)
            results[name][example.id] = {"category": example.category, **score(example, packed)}
        print(
            f"{example.id}: base={results['base'][example.id]['rank']} "
            f"entity+ppr={results['entity+ppr'][example.id]['rank']}",
            flush=True,
        )

    await client.close()
    await engine.dispose()
    categories = sorted({e.category for e in golden})
    report = {
        "methods": {
            name: {
                "overall": summarize(list(rows.values())),
                **{
                    c: summarize([r for r in rows.values() if r["category"] == c])
                    for c in categories
                },
            }
            for name, rows in results.items()
        },
        "examples": results,
        "llm_cost_usd": llm.total_cost_usd if llm else 0.0,
        "llm_latency_ms": {
            name: {"p50": sorted(v)[len(v) // 2], "p90": sorted(v)[int(len(v) * 0.9)]}
            for name, v in timings.items()
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1))  # noqa: ASYNC240
    print(f"wrote {out}; LLM cost ${report['llm_cost_usd']:.4f}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.out, use_llm=not args.no_llm))


if __name__ == "__main__":
    main()
