"""Evaluation CLI.

uv run python -m evals validate                 # golden set vs ingested docs
uv run python -m evals retrieval [--out FILE]   # recall@k / MRR of hybrid search
uv run python -m evals answers [--out FILE] [--answer-model M] [--ids a,b]  # end to end
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
from evals.golden import GOLDEN_DIR, load_golden
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
    overrides = {
        key: value
        for key, value in {
            "model_easy": args.rewrite_model,
            "model_hard": args.answer_model,
            "model_judge": args.judge_model,
        }.items()
        if value
    }
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
    try:
        report = await evaluate_answers(
            golden,
            QueryDeps(index, make_embedder(settings), llm),
            judge_llm,
            models,
            concurrency=args.concurrency,
        )
    finally:
        await client.close()
    report.cost_usd = llm.total_cost_usd
    return report


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m evals")
    parser.add_argument("command", choices=["validate", "retrieval", "answers"])
    parser.add_argument("--golden", type=Path, default=GOLDEN_DIR / "v0.yaml")
    parser.add_argument("--out", type=Path, help="retrieval/answers: write the JSON report here")
    parser.add_argument("--answer-model", help="answers: override MODEL_HARD")
    parser.add_argument("--rewrite-model", help="answers: override MODEL_EASY")
    parser.add_argument("--judge-model", help="answers: override MODEL_JUDGE")
    parser.add_argument("--no-judge", action="store_true", help="answers: skip the judge")
    parser.add_argument("--ids", help="answers: comma-separated example ids only")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    report: RetrievalReport | AnswerReport
    if args.command == "validate":
        code = asyncio.run(run_validate(args.golden))
    else:
        if args.command == "answers":
            report = asyncio.run(run_answers(args.golden, args))
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
