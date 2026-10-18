"""Evaluation CLI.

uv run python -m evals validate                 # golden set vs ingested docs
uv run python -m evals retrieval [--out FILE]   # recall@k / MRR of hybrid search
uv run python -m evals answers [--out FILE] [--answer-model M] [--ids a,b]  # end to end
uv run python -m evals latency [--api URL] [--n 20]   # TTFT through the running chat API
uv run python -m evals context [--rewrite-model M] [--no-expand] [--no-anchors]  # retrieval path
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.config import get_settings
from app.genai.embeddings import make_embedder
from app.genai.llm import make_llm
from app.orchestrator.pipeline import QueryDeps
from app.retrieval import kb_index
from evals.answers import AnswerReport, evaluate_answers
from evals.context import ContextReport, evaluate_context
from evals.golden import GOLDEN_DIR, load_golden
from evals.latency import measure, summarize, to_json
from evals.retrieval import RetrievalReport, evaluate_retrieval
from evals.validate import validate
from shared.db.session import make_engine, make_session_factory


async def run_validate(golden_path: Path) -> int:
    golden = load_golden(golden_path)
    engine = make_engine(get_settings().database_url)
    async with make_session_factory(engine)() as session:
        problems = await validate(session, golden)
    await engine.dispose()
    answerable = sum(1 for e in golden.examples if e.expected_route == "ANSWERED")
    for problem in problems:
        print(f"✗ {problem.example_id}: {problem.message}")
    print(f"{len(golden.examples)} examples, {answerable} answerable, {len(problems)} problems")
    return 1 if problems else 0


async def run_retrieval(golden_path: Path) -> RetrievalReport:
    settings = get_settings()
    golden = load_golden(golden_path)
    client = kb_index.make_client(settings)
    index = kb_index.KbIndex(client, settings.opensearch_index, settings.embedding_dimensions)
    try:
        report = await evaluate_retrieval(golden, index, make_embedder(settings))
    finally:
        await client.close()
    return report


async def run_answers(golden_path: Path, args: argparse.Namespace) -> AnswerReport:
    overrides: dict[str, object] = {
        key: value
        for key, value in {
            "model_easy": args.rewrite_model,
            "model_hard": args.answer_model,
            "model_judge": args.judge_model,
        }.items()
        if value
    }
    overrides["query_expansion"] = not args.no_expand
    if args.rewrite_model:  # the .env effort may not apply to another model
        overrides["model_easy_reasoning"] = args.rewrite_reasoning
    settings = get_settings().model_copy(update=overrides)
    golden = load_golden(golden_path)
    if args.ids:
        wanted = set(args.ids.split(","))
        golden.examples = [e for e in golden.examples if e.id in wanted]
    client = kb_index.make_client(settings)
    index = kb_index.KbIndex(client, settings.opensearch_index, settings.embedding_dimensions)
    llm = make_llm(settings)
    models: dict[str, str] = {task: llm.model_for(task) for task in llm.tasks}
    judge_llm = llm if settings.model_judge and not args.no_judge else None
    engine = make_engine(settings.database_url)
    deps = QueryDeps(
        index,
        make_embedder(settings),
        llm,
        sessions=None if args.no_anchors else make_session_factory(engine),
        expand=settings.query_expansion,
    )
    try:
        report = await evaluate_answers(
            golden,
            deps,
            judge_llm,
            models,
            concurrency=args.concurrency,
        )
    finally:
        await client.close()
        await engine.dispose()
    report.cost_usd = llm.total_cost_usd
    return report


async def run_context(golden_path: Path, args: argparse.Namespace) -> ContextReport:
    overrides: dict[str, object] = {"query_expansion": not args.no_expand}
    if args.rewrite_model:
        overrides["model_easy"] = args.rewrite_model
        overrides["model_easy_reasoning"] = args.rewrite_reasoning
    settings = get_settings().model_copy(update=overrides)
    client = kb_index.make_client(settings)
    index = kb_index.KbIndex(client, settings.opensearch_index, settings.embedding_dimensions)
    engine = make_engine(settings.database_url)
    llm = make_llm(settings)
    deps = QueryDeps(
        index,
        make_embedder(settings),
        llm,
        sessions=None if args.no_anchors else make_session_factory(engine),
        expand=settings.query_expansion,
    )
    label = (
        f"plan `{llm.model_for('rewrite')}` ({settings.model_easy_reasoning or 'default'})"
        f"{'' if settings.query_expansion else ', multi-turn only'}"
        f" · anchors {'off' if args.no_anchors else 'on'}"
    )
    try:
        report = await evaluate_context(load_golden(golden_path), deps, label)
    finally:
        await client.close()
        await engine.dispose()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m evals")
    parser.add_argument(
        "command", choices=["validate", "retrieval", "answers", "latency", "context"]
    )
    parser.add_argument("--golden", type=Path, default=GOLDEN_DIR / "v0.yaml")
    parser.add_argument("--out", type=Path, help="retrieval/answers: write the JSON report here")
    parser.add_argument("--answer-model", help="answers: override MODEL_HARD")
    parser.add_argument("--rewrite-model", help="answers: override MODEL_EASY")
    parser.add_argument("--judge-model", help="answers: override MODEL_JUDGE")
    parser.add_argument("--no-judge", action="store_true", help="answers: skip the judge")
    parser.add_argument("--ids", help="answers: comma-separated example ids only")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--api", default="http://localhost:8000", help="latency: API base URL")
    parser.add_argument("--n", type=int, default=20, help="latency: questions to time")
    parser.add_argument(
        "--rewrite-reasoning", help="reasoning effort for --rewrite-model (default: none sent)"
    )
    parser.add_argument("--no-expand", action="store_true", help="no expansion terms")
    parser.add_argument("--no-anchors", action="store_true", help="no identifier anchors")
    args = parser.parse_args()
    report: RetrievalReport | AnswerReport | ContextReport
    if args.command == "validate":
        code = asyncio.run(run_validate(args.golden))
    elif args.command == "latency":
        samples = measure(load_golden(args.golden), args.api, args.n)
        print(summarize(samples))
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(to_json(samples))
        code = 0
    else:
        if args.command == "answers":
            report = asyncio.run(run_answers(args.golden, args))
        elif args.command == "context":
            report = asyncio.run(run_context(args.golden, args))
        else:
            report = asyncio.run(run_retrieval(args.golden))
        print(report.to_markdown())
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report.to_dict(), indent=2))
            print(f"\nReport written to {args.out}")
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
