"""Answer one user message (docs/PLAN.md §8.6), headless: no persistence or transport.

    rules → version → ┬ plan (cheapest model): standalone query + expansion terms ┐
                      ├ embed the user's turns (kNN vector)                       ├→ search
                      └ identifier anchors (Postgres)                             ┘
          → synthesize (streamed) → validate

The three middle steps run concurrently, so the plan call costs only the time it takes
beyond the embedding (docs/research/graphrag-retrieval-study.md, option B). A failed or
slow plan falls back to the raw turns.

The chat API (Phase 2 · 2/3) wraps this with sessions, SSE and spend limits. The FAQ cache,
response cache, graph expansion (Phase 3) and calibrated confidence (Phase 4) slot in later.
"""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.genai.embeddings import Embedder
from app.genai.llm import ChatModel, Message
from app.orchestrator import prompts
from app.orchestrator.rules import (
    DEFAULT_VERSION,
    EscalationReason,
    detect_version,
    escalation_reason,
)
from app.retrieval.anchors import anchor_pages
from app.retrieval.kb_index import KbIndex
from app.retrieval.search import Passage, search_context

log = structlog.get_logger()

Route = Literal["ANSWERED", "ESCALATE"]
TokenSink = Callable[[str], Awaitable[None]]

PLAN_TIMEOUT_S = 2.5  # past this, search with the raw turns rather than keep the user waiting


@dataclass(frozen=True)
class Citation:
    chunk_id: str
    url: str
    title: str
    versions: list[str]


@dataclass
class AnswerResult:
    route: Route
    version: str
    answer: str = ""
    citations: list[Citation] = field(default_factory=list)
    escalation_reason: EscalationReason | None = None
    search_query: str = ""
    passages: list[Passage] = field(default_factory=list)
    first_token_ms: int | None = None
    latency_ms: int = 0


@dataclass
class QueryDeps:
    index: KbIndex
    embedder: Embedder
    llm: ChatModel
    sessions: async_sessionmaker[AsyncSession] | None = None  # None: no identifier anchors
    expand: bool = True  # plan every turn; False: rewrite multi-turn messages only


async def answer(
    deps: QueryDeps,
    query: str,
    history: list[Message] | None = None,
    *,
    version_hint: str | None = None,
    on_token: TokenSink | None = None,
) -> AnswerResult:
    started = time.monotonic()
    history = history or []
    user_turns = [turn["content"] for turn in history if turn["role"] == "user"]
    version = version_hint or detect_version([*user_turns, query]) or DEFAULT_VERSION

    def done(result: AnswerResult) -> AnswerResult:
        result.latency_ms = int((time.monotonic() - started) * 1000)
        log.info(
            "answer.done",
            route=result.route,
            reason=result.escalation_reason,
            version=version,
            citations=len(result.citations),
            first_token_ms=result.first_token_ms,
            latency_ms=result.latency_ms,
        )
        return result

    reason = escalation_reason(query)
    if reason is not None:
        return done(AnswerResult("ESCALATE", version, escalation_reason=reason))

    search_query, passages = await gather_context(deps, query, history, version)
    result = AnswerResult("ANSWERED", version, search_query=search_query, passages=passages)
    if not passages:
        result.route, result.escalation_reason = "ESCALATE", "out_of_scope"
        return done(result)

    parts: list[str] = []
    async for delta in deps.llm.stream(
        "answer", prompts.answer_messages(query, history, passages, version)
    ):
        if result.first_token_ms is None:
            result.first_token_ms = int((time.monotonic() - started) * 1000)
        parts.append(delta)
        if on_token is not None:
            await on_token(delta)
    result.answer = "".join(parts).strip()

    by_id = {p.chunk_id: p for p in passages}
    cited = prompts.cited_ids(result.answer)
    if prompts.FALLBACK in result.answer or not cited:
        result.route, result.escalation_reason = "ESCALATE", "out_of_scope"
    elif any(chunk_id not in by_id for chunk_id in cited):
        result.route, result.escalation_reason = "ESCALATE", "invalid_citation"
    else:
        result.citations = [
            Citation(c, by_id[c].url, by_id[c].title, by_id[c].versions) for c in cited
        ]
    return done(result)


# Error codes, $operators, versions, snake_case and camelCase names: E11000, $median, 7.0,
# max_time, maxTimeMS.
_IDENTIFIER = re.compile(r"\$?[A-Za-z_]*\d[\w.]*|\$\w+|\b\w+_\w+|\b[a-z]+[A-Z]\w*")


def identifiers(text: str) -> list[str]:
    return list(dict.fromkeys(m.rstrip(".") for m in _IDENTIFIER.findall(text)))


async def gather_context(
    deps: QueryDeps, query: str, history: list[Message], version: str
) -> tuple[str, list[Passage]]:
    """The lexical search text and the packed passages for one message (T2 to T6)."""
    user_turns = [turn["content"] for turn in history if turn["role"] == "user"]
    raw = "\n".join([*user_turns, query])
    vector = asyncio.create_task(deps.embedder.embed([raw]))
    anchors = asyncio.create_task(_anchors(deps, version, raw))
    if deps.expand or history:
        text = await _plan(deps.llm, query, history, raw)
    else:
        text = query
    [embedding] = await vector
    passages = await search_context(
        deps.index, text=text, vector=embedding, version=version, anchors=await anchors
    )
    return text, passages


async def _anchors(deps: QueryDeps, version: str, raw: str) -> list[str]:
    """Anchored pages; an aid, so a lookup failure degrades to none rather than failing."""
    if deps.sessions is None:
        return []
    try:
        return await anchor_pages(deps.sessions, version, raw)
    except Exception:
        log.exception("answer.anchors_failed")
        return []


async def _plan(llm: ChatModel, query: str, history: list[Message], raw: str) -> str:
    """Standalone query + expansion terms (T2) as one lexical search text; ``raw`` (the
    user's turns) when the model fails, is slow, or returns nothing usable.

    Identifiers from the latest message that the plan dropped are appended: follow-ups like
    "E11000" are exactly what lexical search needs, and small models sometimes lose them.
    """
    try:
        reply = await asyncio.wait_for(
            llm.complete(
                "rewrite", prompts.plan_messages(query, history), json_schema=prompts.PLAN_SCHEMA
            ),
            PLAN_TIMEOUT_S,
        )
        parsed = json.loads(reply)
        rewritten = str(parsed["query"]).strip()
        terms = str(parsed.get("terms", "")).strip()
    except (TimeoutError, ValueError, KeyError, TypeError) as exc:
        log.warning("answer.plan_failed", error=type(exc).__name__)
        return raw
    if not rewritten:
        return raw
    missing = [i for i in identifiers(query) if i.lower() not in rewritten.lower()]
    return "\n".join(part for part in (" ".join([rewritten, *missing]), terms) if part)
