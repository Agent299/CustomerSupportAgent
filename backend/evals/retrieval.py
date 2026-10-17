"""Retrieval evaluation: does hybrid search put an expected source in the top k?

Baseline for Phase 1. Phase 2 (query rewriting, reranking) and Phase 3 (graph expansion)
must beat it on the same golden set (docs/PLAN.md §11, §13).
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.genai.embeddings import Embedder
from app.retrieval.kb_index import KbIndex
from evals.golden import GoldenSet
from evals.metrics import first_relevant_rank, mean, recall_at, reciprocal_rank

KS = (1, 3, 5, 10)


@dataclass
class ExampleResult:
    id: str
    category: str
    version: str
    rank: int | None
    top_doc_keys: list[list[str]]


@dataclass
class RetrievalReport:
    golden_set: str
    embedding_model: str
    created_at: str
    examples: list[ExampleResult] = field(default_factory=list)

    def metrics(self, category: str | None = None) -> dict[str, float]:
        rows = [e for e in self.examples if category is None or e.category == category]
        out = {f"recall@{k}": mean([recall_at(e.rank, k) for e in rows]) for k in KS}
        out["mrr@10"] = mean([reciprocal_rank(e.rank, 10) for e in rows])
        out["n"] = float(len(rows))
        return out

    def to_dict(self) -> dict[str, Any]:
        categories = sorted({e.category for e in self.examples})
        return {
            "golden_set": self.golden_set,
            "embedding_model": self.embedding_model,
            "created_at": self.created_at,
            "overall": self.metrics(),
            "by_category": {c: self.metrics(c) for c in categories},
            "examples": [asdict(e) for e in self.examples],
        }

    def to_markdown(self) -> str:
        header = "| Slice | n | " + " | ".join(f"R@{k}" for k in KS) + " | MRR@10 |"
        lines = [
            f"Golden set `{self.golden_set}` · embeddings `{self.embedding_model}` · "
            f"{self.created_at}",
            "",
            header,
            "|" + "---|" * (len(KS) + 3),
        ]
        slices = [("overall", None)] + [(c, c) for c in sorted({e.category for e in self.examples})]
        for label, category in slices:
            m = self.metrics(category)
            cells = " | ".join(f"{m[f'recall@{k}']:.2f}" for k in KS)
            lines.append(f"| {label} | {int(m['n'])} | {cells} | {m['mrr@10']:.2f} |")
        misses = [e for e in self.examples if e.rank is None or e.rank > 5]
        if misses:
            lines += ["", "Not in top 5: " + ", ".join(f"`{e.id}`" for e in misses)]
        return "\n".join(lines)


async def evaluate_retrieval(
    golden: GoldenSet, index: KbIndex, embedder: Embedder, *, k: int = max(KS)
) -> RetrievalReport:
    answerable = [e for e in golden.examples if e.expected_route == "ANSWERED"]
    vectors = await embedder.embed([e.search_text for e in answerable])
    report = RetrievalReport(
        golden_set=golden.name,
        embedding_model=embedder.model,
        created_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    )
    for example, vector in zip(answerable, vectors, strict=True):
        hits = await index.hybrid_search(
            example.search_text, vector, versions=[example.effective_version], k=k
        )
        ranked = [list(hit.source.get("doc_keys", [])) for hit in hits]
        report.examples.append(
            ExampleResult(
                id=example.id,
                category=example.category,
                version=example.effective_version,
                rank=first_relevant_rank(ranked, set(example.expected_sources)),
                top_doc_keys=ranked[:5],
            )
        )
    return report
