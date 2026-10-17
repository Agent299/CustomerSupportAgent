from collections import Counter

import pytest
from pydantic import ValidationError

from evals.golden import GoldenExample, GoldenSet, load_golden
from evals.metrics import first_relevant_rank, mean, recall_at, reciprocal_rank
from evals.retrieval import ExampleResult, RetrievalReport
from evals.validate import check_example, normalize


@pytest.fixture(scope="module")
def golden() -> GoldenSet:
    return load_golden()


def test_golden_v0_loads_with_balanced_categories(golden: GoldenSet) -> None:
    counts = Counter(e.category for e in golden.examples)

    assert len(golden.examples) >= 50
    assert all(counts[c] >= 10 for c in ("faq", "complex", "error_code", "escalation"))


def test_golden_v0_covers_versions_and_multi_turn(golden: GoldenSet) -> None:
    versions = {e.version for e in golden.examples}

    assert {"8.0", "7.0"} <= versions
    assert sum(1 for e in golden.examples if e.history) >= 3


def example(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "x",
        "category": "faq",
        "query": "What port?",
        "expected_route": "ANSWERED",
        "expected_sources": ["reference/default-mongodb-port"],
        "key_facts": ["27017"],
        "golden_answer": "Port 27017 by default.",
    }
    return base | overrides


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"expected_sources": []}, "need sources"),
        ({"key_facts": []}, "need sources"),
        ({"escalation_reason": "billing"}, "only applies"),
        ({"category": "escalation"}, "iff route"),
        (
            {"category": "escalation", "expected_route": "ESCALATE", "escalation_reason": None},
            "have no expected_sources",
        ),
        ({"id": "Bad Id"}, "pattern"),
        ({"unknown": 1}, "Extra inputs"),
    ],
)
def test_invalid_examples_are_rejected(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        GoldenExample.model_validate(example(**overrides))


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        GoldenSet.model_validate(
            {"name": "n", "description": "d", "examples": [example(), example()]}
        )


def test_search_text_includes_earlier_user_turns() -> None:
    ex = GoldenExample.model_validate(
        example(
            query="E11000",
            history=[
                {"role": "user", "content": "My app isn't connecting."},
                {"role": "assistant", "content": "What error code do you see?"},
            ],
        )
    )

    assert ex.search_text == "My app isn't connecting.\nE11000"
    assert ex.effective_version == "9.0"


def test_normalize_ignores_links_code_and_case() -> None:
    raw = "A replica set is a group of [`mongod`](https://x/mongod.md#y) instances"

    assert normalize(raw) == "a replica set is a group of mongod instances"


def test_check_example_reports_missing_sources_and_facts() -> None:
    ex = GoldenExample.model_validate(
        example(expected_sources=["a", "b"], key_facts=["Port 27017", "missing fact"])
    )

    problems = check_example(ex, {"a": normalize("Listens on **port `27017`**.")})

    assert [p.message for p in problems] == [
        "source 'b' not found in 9.0",
        "key fact not in any expected source: 'missing fact'",
    ]


def test_ranking_metrics() -> None:
    ranked = [["other"], ["x", "core/index-unique"], ["core/index-unique"]]

    rank = first_relevant_rank(ranked, {"core/index-unique"})

    assert rank == 2
    assert (recall_at(rank, 1), recall_at(rank, 3)) == (0.0, 1.0)
    assert reciprocal_rank(rank, 10) == 0.5
    assert first_relevant_rank(ranked, {"nope"}) is None
    assert recall_at(None, 10) == reciprocal_rank(None, 10) == 0.0
    assert mean([]) == 0.0


def test_report_aggregates_by_category() -> None:
    report = RetrievalReport("g", "m", "now")
    report.examples = [
        ExampleResult("a", "faq", "9.0", 1, []),
        ExampleResult("b", "faq", "9.0", None, []),
        ExampleResult("c", "complex", "9.0", 4, []),
    ]

    assert report.metrics("faq")["recall@1"] == 0.5
    assert report.metrics()["recall@5"] == pytest.approx(2 / 3)
    assert report.metrics("complex")["mrr@10"] == 0.25
    markdown = report.to_markdown()
    assert "| overall | 3 |" in markdown and "`b`" in markdown


@pytest.mark.integration
async def test_validate_against_ingested_pages(sessions) -> None:  # type: ignore[no-untyped-def]
    from evals.validate import validate
    from shared.sources import DocVersion
    from workers.ingest.chunker import chunk_markdown
    from workers.ingest.store import mark_deleted, upsert_page

    v9 = DocVersion("9.0", "manual")
    page = "# Port\n\n## Default\n\nmongod listens on port [`27017`](https://x.md) by default.\n"
    async with sessions.begin() as session:
        await upsert_page(
            session,
            key="reference/default-mongodb-port",
            version=v9,
            parsed=chunk_markdown(page, count_tokens=lambda t: len(t.split())),
            content_hash="h",
        )
    golden = GoldenSet.model_validate(
        {
            "name": "t",
            "description": "t",
            "examples": [
                example(id="ok", key_facts=["port 27017"]),
                example(id="bad-fact", key_facts=["port 8080"]),
                example(id="v7", version="7.0"),  # page only ingested for 9.0
            ],
        }
    )

    async with sessions() as session:
        problems = await validate(session, golden)

    assert {(p.example_id, p.message.split(":")[0]) for p in problems} == {
        ("bad-fact", "key fact not in any expected source"),
        ("v7", "source 'reference/default-mongodb-port' not found in 7.0"),
        ("v7", "key fact not in any expected source"),
    }

    async with sessions.begin() as session:  # deleted pages don't count as sources
        await mark_deleted(session, v9, "reference/default-mongodb-port")
    async with sessions() as session:
        assert any(p.example_id == "ok" for p in await validate(session, golden))
