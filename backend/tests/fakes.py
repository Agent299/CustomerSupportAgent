"""Test doubles for the query stack: no services, no API calls."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from app.genai.llm import Message, Task
from app.retrieval.kb_index import Hit


class FakeLlm:
    def __init__(
        self,
        answer: str,
        rewrite: str = '{"query": "standalone", "terms": ""}',
        cost_usd: float = 0.0,
        plan_delay_s: float = 0.0,
    ) -> None:
        self.answer = answer
        self.rewrite = rewrite  # the search plan's JSON reply
        self.total_cost_usd = cost_usd  # what LlmClient reports after a turn
        self.plan_delay_s = plan_delay_s
        self.calls: list[tuple[Task, list[Message]]] = []

    def model_for(self, task: Task) -> str:
        return f"fake-{task}"

    async def complete(
        self, task: Task, messages: list[Message], *, json_schema: dict[str, Any] | None = None
    ) -> str:
        self.calls.append((task, messages))
        await asyncio.sleep(self.plan_delay_s)
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
        self.anchored: list[list[str]] = []  # doc_keys of page-limited searches

    async def hybrid_search(
        self,
        query: str,
        vector: list[float],
        *,
        versions: list[str] | None = None,
        k: int = 40,
        doc_keys: list[str] | None = None,
    ) -> list[Hit]:
        if doc_keys is not None:
            self.anchored.append(doc_keys)
            return [h for h in self.hits if set(h.source["doc_keys"]) & set(doc_keys)]
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
