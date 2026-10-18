"""Query core with fakes: retrieval packing, citations, routing. No services, no API calls."""

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from app.genai.llm import LlmClient, Message, Task, TaskConfig, Usage
from app.orchestrator import pipeline, prompts
from app.orchestrator.pipeline import QueryDeps
from app.retrieval.kb_index import Hit
from app.retrieval.search import Passage, diversify, to_passage

ID_A, ID_B, ID_C = "a" * 16, "b" * 16, "c" * 16


def passage(chunk_id: str, page: str, tokens: int = 100) -> Passage:
    return Passage(chunk_id, 1.0, f"text {chunk_id}", "Crumb", "Title", [page], ["9.0"], "", tokens)


def test_diversify_caps_chunks_per_page_and_keeps_rank_order() -> None:
    ranked = [
        passage("1" * 16, "p1"),
        passage("2" * 16, "p1"),
        passage("3" * 16, "p1"),
        passage("4" * 16, "p2"),
    ]

    kept = diversify(ranked, max_per_page=2)

    assert [p.chunk_id[0] for p in kept] == ["1", "2", "4"]


def test_diversify_respects_token_budget_but_always_keeps_the_top_passage() -> None:
    ranked = [
        passage("1" * 16, "p1", 900),
        passage("2" * 16, "p2", 200),
        passage("3" * 16, "p3", 50),
    ]

    assert [p.chunk_id[0] for p in diversify(ranked, token_budget=500)] == ["1"]
    assert [p.chunk_id[0] for p in diversify(ranked, token_budget=960)] == ["1", "3"]


def test_to_passage_builds_a_versioned_citation_url() -> None:
    hit = Hit(
        ID_A,
        0.9,
        {
            "text": "t",
            "breadcrumb": "Unique Indexes > Behavior",
            "titles": ["Unique Indexes"],
            "doc_keys": ["core/index-unique"],
            "versions": ["7.0", "8.0"],
            "token_count": 42,
        },
    )

    p = to_passage(hit, "7.0")

    assert p.url == "https://www.mongodb.com/docs/v7.0/core/index-unique/"
    assert (p.title, p.page, p.token_count) == ("Unique Indexes", "core/index-unique", 42)


def test_cited_ids_dedupes_in_order_and_ignores_malformed() -> None:
    answer = f"Yes [c:{ID_B}]. Also [c:{ID_A}] and again [c:{ID_B}] but not [c:xyz] or [{ID_C}]."
    assert prompts.cited_ids(answer) == [ID_B, ID_A]


def test_context_is_delimited_with_ids() -> None:
    context = prompts.format_context([passage(ID_A, "p1")])
    assert context.startswith("<context>") and context.endswith("</context>")
    assert f'id="c:{ID_A}"' in context


class FakeLlm:
    def __init__(self, answer: str, rewrite: str = '{"query": "standalone"}') -> None:
        self.answer = answer
        self.rewrite = rewrite
        self.calls: list[tuple[Task, list[Message]]] = []

    def model_for(self, task: Task) -> str:
        return f"fake-{task}"

    async def complete(
        self, task: Task, messages: list[Message], *, json_schema: dict[str, Any] | None = None
    ) -> str:
        self.calls.append((task, messages))
        return self.rewrite

    async def stream(self, task: Task, messages: list[Message]) -> AsyncIterator[str]:
        self.calls.append((task, messages))
        for word in self.answer.split(" "):
            yield word + " "


class FakeEmbedder:
    model = "fake"
    dimensions = 3

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 0.0, 1.0] for _ in texts]


class FakeIndex:
    def __init__(self, hits: list[Hit]) -> None:
        self.hits = hits
        self.queries: list[tuple[str, list[str] | None]] = []

    async def hybrid_search(
        self, query: str, vector: list[float], *, versions: list[str] | None = None, k: int = 40
    ) -> list[Hit]:
        self.queries.append((query, versions))
        return self.hits


def hit(chunk_id: str, page: str) -> Hit:
    source = {
        "text": "Unique indexes reject duplicates.",
        "breadcrumb": "Unique Indexes",
        "titles": ["Unique Indexes"],
        "doc_keys": [page],
        "versions": ["8.0", "9.0"],
        "token_count": 50,
    }
    return Hit(chunk_id, 1.0, source)


def deps(llm: FakeLlm, hits: list[Hit] | None = None) -> QueryDeps:
    index = FakeIndex([hit(ID_A, "core/index-unique")] if hits is None else hits)
    return QueryDeps(index=index, embedder=FakeEmbedder(), llm=llm)  # type: ignore[arg-type]


async def test_answers_with_validated_citations_and_streams_tokens() -> None:
    llm = FakeLlm(f"A unique index rejects duplicates [c:{ID_A}].")
    streamed: list[str] = []

    async def sink(token: str) -> None:
        streamed.append(token)

    result = await pipeline.answer(deps(llm), "What does E11000 mean?", on_token=sink)

    assert result.route == "ANSWERED"
    assert [c.chunk_id for c in result.citations] == [ID_A]
    assert result.citations[0].url.endswith("/docs/manual/core/index-unique/")
    assert "".join(streamed).strip() == result.answer
    assert result.first_token_ms is not None
    assert [task for task, _ in llm.calls] == ["answer"]  # turn 1: no rewrite


async def test_rule_escalation_skips_retrieval_and_models() -> None:
    llm = FakeLlm("unused")
    d = deps(llm)

    result = await pipeline.answer(d, "I was charged twice, I want a refund")

    assert (result.route, result.escalation_reason) == ("ESCALATE", "billing")
    assert llm.calls == []
    assert d.index.queries == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (prompts.FALLBACK, "out_of_scope"),
        ("Unique indexes reject duplicates.", "out_of_scope"),  # no citation
        (f"Unique indexes reject duplicates [c:{ID_C}].", "invalid_citation"),
    ],
)
async def test_unsupported_answers_escalate(answer: str, reason: str) -> None:
    result = await pipeline.answer(deps(FakeLlm(answer)), "What does E11000 mean?")
    assert (result.route, result.escalation_reason, result.citations) == ("ESCALATE", reason, [])


async def test_no_passages_escalates_without_calling_the_model() -> None:
    llm = FakeLlm("unused")
    result = await pipeline.answer(deps(llm, hits=[]), "How do I tune nginx?")
    assert (result.route, result.escalation_reason) == ("ESCALATE", "out_of_scope")
    assert llm.calls == []


async def test_multi_turn_rewrites_and_uses_the_version_from_history() -> None:
    llm = FakeLlm(
        f"Use $median [c:{ID_A}].",
        rewrite=json.dumps({"query": "How do I compute a median in MongoDB 7.0?"}),
    )
    d = deps(llm)
    history: list[Message] = [
        {"role": "user", "content": "We're running MongoDB 7.0."},
        {"role": "assistant", "content": "Noted. What do you need?"},
    ]

    result = await pipeline.answer(d, "How do I compute a median?", history)

    rewritten = "How do I compute a median in MongoDB 7.0?"
    assert (result.version, result.search_query) == ("7.0", rewritten)
    assert d.index.queries == [(rewritten, ["7.0"])]  # type: ignore[attr-defined]
    answer_messages = llm.calls[-1][1]
    assert answer_messages[1:3] == history  # earlier turns reach the answer model


async def test_rewrite_that_drops_the_latest_message_gets_it_appended() -> None:
    llm = FakeLlm(f"ok [c:{ID_A}]", rewrite=json.dumps({"query": "app connection error"}))
    history: list[Message] = [{"role": "user", "content": "My app isn't connecting."}]

    result = await pipeline.answer(deps(llm), "E11000", history)

    assert result.search_query == "app connection error E11000"


def test_identifiers_finds_codes_operators_versions_and_api_names() -> None:
    text = "On 7.0, $group fails with E11000; maxTimeMS and max_time_ms? How do I fix it."
    assert pipeline.identifiers(text) == ["7.0", "$group", "E11000", "maxTimeMS", "max_time_ms"]


async def test_unparseable_rewrite_falls_back_to_the_user_turns() -> None:
    llm = FakeLlm(f"ok [c:{ID_A}]", rewrite="not json")
    d = deps(llm)
    history: list[Message] = [{"role": "user", "content": "My app isn't connecting."}]

    result = await pipeline.answer(d, "E11000", history)

    assert result.search_query == "My app isn't connecting.\nE11000"


async def test_version_hint_wins_over_text() -> None:
    result = await pipeline.answer(
        deps(FakeLlm(f"ok [c:{ID_A}]")), "On 7.0 how do I x?", version_hint="8.0"
    )
    assert result.version == "8.0"


class FakeCompletions:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        usage = SimpleNamespace(
            prompt_tokens=1_000_000,
            completion_tokens=100_000,
            prompt_tokens_details=SimpleNamespace(cached_tokens=200_000),
        )
        message = SimpleNamespace(content='{"query": "q"}')
        return SimpleNamespace(
            model="gpt-5-nano-2026-08-07", usage=usage, choices=[SimpleNamespace(message=message)]
        )


async def test_llm_client_meters_cost_by_configured_model() -> None:
    completions = FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    llm = LlmClient(client, {"rewrite": TaskConfig("gpt-5-nano", 300, "minimal")})  # type: ignore[arg-type]

    await llm.complete(
        "rewrite", [{"role": "user", "content": "x"}], json_schema={"type": "object"}
    )

    assert completions.kwargs["model"] == "gpt-5-nano"
    assert completions.kwargs["reasoning_effort"] == "minimal"
    assert completions.kwargs["response_format"]["json_schema"]["strict"] is True
    [usage] = llm.usage
    assert usage.model == "gpt-5-nano"  # dated snapshot priced as its alias
    # 800k uncached x $0.05 + 200k cached x $0.005 + 100k out x $0.40, per 1M tokens
    assert usage.cost_usd == pytest.approx(0.04 + 0.001 + 0.04)
    assert llm.total_cost_usd == pytest.approx(0.081)


async def test_unconfigured_task_fails_clearly() -> None:
    llm = LlmClient(SimpleNamespace(), {})  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="No model configured for task 'answer'"):
        await llm.complete("answer", [])


def test_unknown_model_cost_is_none() -> None:
    assert Usage("answer", "some-new-model", 10, 0, 10).cost_usd is None
