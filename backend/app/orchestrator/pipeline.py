"""Answer one user message (docs/PLAN.md §8.6), headless: no persistence or transport.

    rules → version → rewrite (multi-turn only) → retrieve → synthesize (streamed) → validate

The chat API (Phase 2 · 2/3) wraps this with sessions, SSE and spend limits. The FAQ cache,
response cache, graph expansion (Phase 3) and calibrated confidence (Phase 4) slot in later.
"""

import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

import structlog

from app.genai.embeddings import Embedder
from app.genai.llm import ChatModel, Message
from app.orchestrator import prompts
from app.orchestrator.rules import (
    DEFAULT_VERSION,
    EscalationReason,
    detect_version,
    escalation_reason,
)
from app.retrieval.kb_index import KbIndex
from app.retrieval.search import Passage, retrieve

log = structlog.get_logger()

Route = Literal["ANSWERED", "ESCALATE"]
TokenSink = Callable[[str], Awaitable[None]]


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

    search_query = await _standalone_query(deps.llm, query, history) if history else query
    passages = await retrieve(deps.index, deps.embedder, search_query, version)
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


async def _standalone_query(llm: ChatModel, query: str, history: list[Message]) -> str:
    """Resolve references to earlier turns (T2). Falls back to the raw turns on bad output.

    Identifiers from the latest message that the rewrite dropped are appended: follow-ups
    like "E11000" are exactly what lexical search needs, and small models sometimes lose them.
    """
    raw = await llm.complete(
        "rewrite", prompts.rewrite_messages(query, history), json_schema=prompts.REWRITE_SCHEMA
    )
    try:
        rewritten = str(json.loads(raw)["query"]).strip()
    except (ValueError, KeyError, TypeError):
        rewritten = ""
    if rewritten:
        missing = [i for i in identifiers(query) if i.lower() not in rewritten.lower()]
        return " ".join([rewritten, *missing])
    log.warning("answer.rewrite_unparsed", raw=raw[:200])
    user_turns = [turn["content"] for turn in history if turn["role"] == "user"]
    return "\n".join([*user_turns, query])
