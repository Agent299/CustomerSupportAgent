"""Evaluation CLI.

uv run python -m evals validate                 # golden set vs ingested docs
uv run python -m evals retrieval [--out FILE]   # recall@k / MRR of hybrid search
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.config import get_settings
from app.genai.embeddings import make_embedder
from app.retrieval import kb_index
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m evals")
    parser.add_argument("command", choices=["validate", "retrieval"])
    parser.add_argument("--golden", type=Path, default=GOLDEN_DIR / "v0.yaml")
    parser.add_argument("--out", type=Path, help="retrieval: write the JSON report here")
    args = parser.parse_args()
    if args.command == "validate":
        code = asyncio.run(run_validate(args.golden))
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
