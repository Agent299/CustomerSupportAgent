"""End-to-end answer evaluation on the golden set (docs/PLAN.md §11).

Runs the real query pipeline per example and scores, deterministically: routing, escalation
recall, context recall (an expected page reached the model), citation hit (the answer cites
one) and time to first token; with a judge model: correctness 1-5 against the golden answer
(the chapter's rubric), with the cited passages as the authority where the reference is less
precise. Used to pick the cheapest model that passes each task (§5).
"""

import asyncio
import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.genai.llm import ChatModel, Message
from app.orchestrator.pipeline import AnswerResult, QueryDeps, answer
from evals.golden import GoldenExample, GoldenSet
from evals.metrics import mean

JUDGE_SYSTEM = """\
You grade a support assistant's answer to a MongoDB question. You get a reference answer \
written from the official documentation, the facts a correct answer should contain, and \
the documentation passages the assistant cited.

The documentation is the authority. The reference is a guide to what a good answer \
covers, but it is shorter and may be less precise than the docs: where the cited passages \
state something more precisely than the reference, or differently, an answer that follows \
the passages is correct. A claim that the passages or the reference contradict is an \
error. Grade correctness only: a true statement that the passages happen not to cover is \
not an error (groundedness is measured separately), and naming the MongoDB version the \
question is about is context, not a claim.

Score correctness from 1 to 5:
5 = correct and complete: covers the reference's key points, nothing wrong.
4 = correct, minor omission or imprecision that would not mislead the user.
3 = partially correct: misses an important point or includes a minor error.
2 = mostly wrong or unhelpful, or a significant error.
1 = wrong, harmful, or does not address the question.
Extra correct detail beyond the reference is fine. Citation markers like [c:...] are \
expected; ignore them. Judge facts, not style."""

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"reason": {"type": "string"}, "score": {"type": "integer"}},
    "required": ["reason", "score"],
    "additionalProperties": False,
}

PASS_SCORE = 4


@dataclass
class ExampleOutcome:
    id: str
    category: str
    expected_route: str
    route: str
    escalation_reason: str | None
    version: str
    search_query: str
    context_hit: bool | None  # answerable only
    citation_hit: bool | None  # answerable and answered only
    judge_score: int | None
    judge_reason: str | None
    first_token_ms: int | None
    latency_ms: int
    answer: str
    cited_pages: list[str]
    context_pages: list[str]

    @property
    def route_correct(self) -> bool:
        return self.route == self.expected_route


@dataclass
class AnswerReport:
    golden_set: str
    models: dict[str, str]
    created_at: str
    cost_usd: float = 0.0
    examples: list[ExampleOutcome] = field(default_factory=list)

    def metrics(self, category: str | None = None) -> dict[str, float | None]:
        rows = [e for e in self.examples if category is None or e.category == category]
        answerable = [e for e in rows if e.expected_route == "ANSWERED"]
        escalate = [e for e in rows if e.expected_route == "ESCALATE"]
        answered = [e for e in answerable if e.route == "ANSWERED"]
        scores = [e.judge_score for e in answered if e.judge_score is not None]
        ttft = sorted(e.first_token_ms for e in rows if e.first_token_ms is not None)

        def rate(flags: list[bool]) -> float | None:
            return mean([float(f) for f in flags]) if flags else None

        return {
            "n": float(len(rows)),
            "route_accuracy": rate([e.route_correct for e in rows]),
            "escalation_recall": rate([e.route == "ESCALATE" for e in escalate]),
            "answer_rate": rate([e.route == "ANSWERED" for e in answerable]),
            "context_recall": rate([bool(e.context_hit) for e in answerable]),
            "citation_hit": rate([bool(e.citation_hit) for e in answered]),
            "judge_mean": statistics.fmean(scores) if scores else None,
            # Answerable examples answered with a passing grade; escalations count as misses.
            "correct_rate": rate(
                [(e.judge_score or 0) >= PASS_SCORE and e.route == "ANSWERED" for e in answerable]
            ),
            "ttft_p50_ms": _percentile(ttft, 50),
            "ttft_p90_ms": _percentile(ttft, 90),
        }

    def to_dict(self) -> dict[str, Any]:
        categories = sorted({e.category for e in self.examples})
        return {
            "golden_set": self.golden_set,
            "models": self.models,
            "created_at": self.created_at,
            "cost_usd": round(self.cost_usd, 4),
            "overall": self.metrics(),
            "by_category": {c: self.metrics(c) for c in categories},
            "examples": [asdict(e) for e in self.examples],
        }

    def to_markdown(self) -> str:
        cols = [
            ("route_accuracy", "Route"),
            ("context_recall", "Ctx recall"),
            ("answer_rate", "Answered"),
            ("citation_hit", "Cite hit"),
            ("judge_mean", "Judge"),
            ("correct_rate", "Correct"),
            ("ttft_p90_ms", "TTFT p90"),
        ]
        models = " · ".join(f"{task} `{model}`" for task, model in self.models.items())
        lines = [
            f"Golden set `{self.golden_set}` · {models} · {self.created_at} · "
            f"cost ${self.cost_usd:.3f}",
            "",
            "| Slice | n | " + " | ".join(label for _, label in cols) + " |",
            "|" + "---|" * (len(cols) + 2),
        ]
        slices = [("overall", None)] + [(c, c) for c in sorted({e.category for e in self.examples})]
        for label, category in slices:
            m = self.metrics(category)
            cells = " | ".join(_fmt(m[key], key) for key, _ in cols)
            lines.append(f"| {label} | {int(m['n'] or 0)} | {cells} |")
        m = self.metrics()
        lines.append(f"\nEscalation recall: {_fmt(m['escalation_recall'], 'rate')}")
        wrong = [e for e in self.examples if not e.route_correct]
        if wrong:
            lines.append(
                "Wrong route: "
                + ", ".join(f"`{e.id}` ({e.route}/{e.escalation_reason})" for e in wrong)
            )
        low = [e for e in self.examples if e.judge_score is not None and e.judge_score < PASS_SCORE]
        if low:
            lines.append("Judge < 4: " + ", ".join(f"`{e.id}` ({e.judge_score})" for e in low))
        return "\n".join(lines)


def _percentile(values: list[int], pct: int) -> float | None:
    if not values:
        return None
    rank = max(0, min(len(values) - 1, round(pct / 100 * len(values) + 0.5) - 1))
    return float(values[rank])


def _fmt(value: float | None, key: str) -> str:
    if value is None:
        return "-"
    if key.endswith("_ms"):
        return f"{value / 1000:.1f}s"
    return f"{value:.2f}"


async def judge(llm: ChatModel, example: GoldenExample, result: AnswerResult) -> tuple[int, str]:
    cited = {c.chunk_id for c in result.citations}
    passages = "\n\n".join(
        f'<passage id="c:{p.chunk_id}" title="{p.title}" section="{p.breadcrumb}">\n'
        f"{p.text}\n</passage>"
        for p in result.passages
        if p.chunk_id in cited
    )
    messages: list[Message] = [
        {"role": "system", "content": JUDGE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Question (MongoDB {example.effective_version}): {example.search_text}\n\n"
                f"Reference answer: {example.golden_answer}\n\n"
                f"Facts the answer should contain: {json.dumps(example.key_facts)}\n\n"
                f"Cited documentation:\n{passages or '(none)'}\n\n"
                f"Assistant answer:\n{result.answer}"
            ),
        },
    ]
    raw = await llm.complete("judge", messages, json_schema=JUDGE_SCHEMA)
    parsed = json.loads(raw)
    return max(1, min(5, int(parsed["score"]))), str(parsed["reason"])


async def evaluate_example(
    deps: QueryDeps, judge_llm: ChatModel | None, example: GoldenExample
) -> ExampleOutcome:
    history: list[Message] = [{"role": t.role, "content": t.content} for t in example.history]
    result = await answer(deps, example.query, history, version_hint=example.version)
    expected = set(example.expected_sources)
    answerable = example.expected_route == "ANSWERED"
    by_id = {p.chunk_id: p for p in result.passages}
    cited_pages = sorted({k for c in result.citations for k in by_id[c.chunk_id].doc_keys})
    context_pages = list(dict.fromkeys(p.page for p in result.passages))
    score = reason = None
    if answerable and result.route == "ANSWERED" and judge_llm is not None:
        score, reason = await judge(judge_llm, example, result)
    return ExampleOutcome(
        id=example.id,
        category=example.category,
        expected_route=example.expected_route,
        route=result.route,
        escalation_reason=result.escalation_reason,
        version=result.version,
        search_query=result.search_query,
        context_hit=bool(expected & {k for p in result.passages for k in p.doc_keys})
        if answerable
        else None,
        citation_hit=bool(expected & set(cited_pages))
        if answerable and result.route == "ANSWERED"
        else None,
        judge_score=score,
        judge_reason=reason,
        first_token_ms=result.first_token_ms,
        latency_ms=result.latency_ms,
        answer=result.answer,
        cited_pages=cited_pages,
        context_pages=context_pages,
    )


async def evaluate_answers(
    golden: GoldenSet,
    deps: QueryDeps,
    judge_llm: ChatModel | None,
    models: dict[str, str],
    *,
    concurrency: int = 4,
) -> AnswerReport:
    report = AnswerReport(
        golden_set=golden.name,
        models=models,
        created_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    )
    gate = asyncio.Semaphore(concurrency)

    async def run(example: GoldenExample) -> ExampleOutcome:
        async with gate:
            return await evaluate_example(deps, judge_llm, example)

    report.examples = list(await asyncio.gather(*(run(e) for e in golden.examples)))
    return report
