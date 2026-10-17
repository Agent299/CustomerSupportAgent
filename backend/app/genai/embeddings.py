"""Text embeddings (docs/PLAN.md §5: cheapest capable model, text-embedding-3-small first)."""

import hashlib
import math
import re
from typing import Protocol

import structlog
from openai import AsyncOpenAI

from app.config import Settings

log = structlog.get_logger()

# OpenAI limits: ≤ 2048 inputs and ≤ 300k tokens per request; chunks are ≤ 512 tokens.
_BATCH_SIZE = 256


class Embedder(Protocol):
    model: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    def __init__(self, client: AsyncOpenAI, model: str, dimensions: int) -> None:
        self._client = client
        self.model = model
        self.dimensions = dimensions
        self.total_tokens = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            batch = texts[start : start + _BATCH_SIZE]
            response = await self._client.embeddings.create(
                input=batch, model=self.model, dimensions=self.dimensions
            )
            self.total_tokens += response.usage.total_tokens
            ordered = sorted(response.data, key=lambda item: item.index)
            vectors.extend(item.embedding for item in ordered)
        log.info("embeddings.batch", model=self.model, texts=len(texts), tokens=self.total_tokens)
        return vectors


_WORD = re.compile(r"[a-z0-9_$.]+")


class HashEmbedder:
    """Deterministic bag-of-words hashing vectors: no API key, no cost.

    Lexically similar texts get similar vectors, which is enough for tests and offline
    demos of the pipeline. Never used for real retrieval quality.
    """

    model = "hash-bow-v1"

    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for word in _WORD.findall(text.lower()):
            digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[bucket] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


def make_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "hash":
        return HashEmbedder(settings.embedding_dimensions)
    if settings.openai_api_key is None:
        raise RuntimeError("OPENAI_API_KEY is required when EMBEDDING_PROVIDER=openai")
    client = AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(), max_retries=5, timeout=60
    )
    return OpenAIEmbedder(client, settings.embedding_model, settings.embedding_dimensions)


def embedding_input(breadcrumb: str, text: str) -> str:
    """What gets embedded for a chunk (ADR 0004). Entities are appended in Phase 3."""
    return f"{breadcrumb}\n\n{text}" if breadcrumb else text
