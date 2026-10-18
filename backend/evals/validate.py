"""Check the golden set against the ingested knowledge base.

For each answerable example: every expected source must be an active page in the example's
version, and every key fact must appear in the text of at least one expected source.
Run after ingestion: ``python -m evals validate``.
"""

import re
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from evals.golden import GoldenExample, GoldenSet
from shared.db.models import Chunk, ChunkOccurrence, Document

_SPACE = re.compile(r"\s+")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def normalize(text: str) -> str:
    """Case/whitespace-insensitive; ignores Markdown links (keeps their text), code and
    emphasis markers, so a fact can quote the prose as a reader sees it."""
    text = _LINK.sub(r"\1", text).replace("`", "").replace("*", "")
    return _SPACE.sub(" ", text).strip().lower()


@dataclass(frozen=True)
class Problem:
    example_id: str
    message: str


async def page_texts(session: AsyncSession, version: str, doc_keys: list[str]) -> dict[str, str]:
    """doc_key → normalized full text of the page (its chunks joined)."""
    rows = await session.execute(
        select(Document.doc_key, Chunk.text)
        .join(ChunkOccurrence, ChunkOccurrence.document_id == Document.id)
        .join(Chunk, Chunk.id == ChunkOccurrence.chunk_id)
        .where(
            Document.version == version,
            Document.status == "active",
            Document.doc_key.in_(doc_keys),
        )
        .order_by(Document.doc_key, ChunkOccurrence.ordinal)
    )
    parts: dict[str, list[str]] = defaultdict(list)
    for doc_key, text in rows:
        parts[doc_key].append(text)
    return {key: normalize("\n".join(texts)) for key, texts in parts.items()}


def check_example(example: GoldenExample, texts: dict[str, str]) -> list[Problem]:
    problems = [
        Problem(example.id, f"source {key!r} not found in {example.effective_version}")
        for key in example.expected_sources
        if key not in texts
    ]
    for fact in example.key_facts:
        needle = normalize(fact)
        if not any(needle in text for text in texts.values()):
            problems.append(Problem(example.id, f"key fact not in any expected source: {fact!r}"))
    return problems


async def validate(session: AsyncSession, golden: GoldenSet) -> list[Problem]:
    problems: list[Problem] = []
    for example in golden.examples:
        if example.expected_route != "ANSWERED":
            continue
        texts = await page_texts(session, example.effective_version, example.expected_sources)
        problems.extend(check_example(example, texts))
    return problems
