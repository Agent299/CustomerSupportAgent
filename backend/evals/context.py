"""What reaches the answer model: the live retrieval path scored on the golden set.

For each answerable example, ``gather_context`` (plan + anchors + hybrid search + packing)
runs exactly as in a chat turn. Scored deterministically, no judge:

- context recall: an expected page is in the packed context;
- fact recall: the share of the example's key facts (verbatim doc quotes) in the packed
  text, the closest proxy for "could the model have said it";
- retrieval time: from the start of the turn to packed passages.
"""

import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from app.genai.llm import Message
from app.orchestrator.pipeline import QueryDeps, gather_context
from evals.golden import GoldenSet
from evals.metrics import mean
from evals.validate import normalize


@dataclass
class ContextOutcome:
    id: str
    category: str
    context_hit: bool
    fact_recall: float
    retrieval_ms: float
    search_text: str
    pages: list[str]


@dataclass
class ContextReport:
    label: str
    examples: list[ContextOutcome] = field(default_factory=list)

    def metrics(self, category: str | None = None) -> dict[str, float]:
        rows = [e for e in self.examples if category is None or e.category == category]
        times = sorted(e.retrieval_ms for e in rows)
        return {
            "n": float(len(rows)),
            "context_recall": mean([float(e.context_hit) for e in rows]),
            "fact_recall": mean([e.fact_recall for e in rows]),
            "retrieval_p50_ms": statistics.median(times),
            "retrieval_p90_ms": times[min(len(times) - 1, int(0.9 * len(times)))],
        }

    def to_dict(self) -> dict[str, Any]:
        categories = sorted({e.category for e in self.examples})
        return {
            "label": self.label,
            "overall": self.metrics(),
            "by_category": {c: self.metrics(c) for c in categories},
            "examples": [e.__dict__ for e in self.examples],
        }

    def to_markdown(self) -> str:
        lines = [
            f"Context eval · {self.label}",
            "",
            "| Slice | n | Context recall | Fact recall | Retrieval p50 | p90 |",
            "|---|---|---|---|---|---|",
        ]
        slices = [("overall", None)] + [(c, c) for c in sorted({e.category for e in self.examples})]
        for label, category in slices:
            m = self.metrics(category)
            lines.append(
                f"| {label} | {int(m['n'])} | {m['context_recall']:.2f} | {m['fact_recall']:.2f} "
                f"| {m['retrieval_p50_ms'] / 1000:.2f}s | {m['retrieval_p90_ms'] / 1000:.2f}s |"
            )
        missed = [e.id for e in self.examples if not e.context_hit]
        if missed:
            lines.append("\nNo expected page in context: " + ", ".join(f"`{i}`" for i in missed))
        return "\n".join(lines)


async def evaluate_context(golden: GoldenSet, deps: QueryDeps, label: str) -> ContextReport:
    """Sequential, so retrieval times are not inflated by concurrent requests."""
    report = ContextReport(label)
    for example in golden.examples:
        if example.expected_route != "ANSWERED":
            continue
        history: list[Message] = [{"role": t.role, "content": t.content} for t in example.history]
        started = time.perf_counter()
        text, passages = await gather_context(
            deps, example.query, history, example.effective_version
        )
        elapsed = (time.perf_counter() - started) * 1000
        packed = normalize("\n".join(p.text for p in passages))
        facts = [normalize(f) in packed for f in example.key_facts]
        pages = list(dict.fromkeys(p.page for p in passages))
        report.examples.append(
            ContextOutcome(
                id=example.id,
                category=example.category,
                context_hit=bool(
                    set(example.expected_sources) & {k for p in passages for k in p.doc_keys}
                ),
                fact_recall=sum(facts) / len(facts),
                retrieval_ms=elapsed,
                search_text=text,
                pages=pages,
            )
        )
    return report
