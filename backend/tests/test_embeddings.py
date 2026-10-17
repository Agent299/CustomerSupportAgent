import math
from types import SimpleNamespace
from typing import Any

from app.genai.embeddings import HashEmbedder, OpenAIEmbedder, embedding_input


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_hash_embedder_is_deterministic_normalized_and_lexical() -> None:
    embedder = HashEmbedder(dimensions=128)

    [dup1, dup2, related, unrelated] = await embedder.embed(
        [
            "E11000 duplicate key error",
            "E11000 duplicate key error",
            "duplicate key error on unique index",
            "configure TLS certificates for mongod",
        ]
    )

    assert dup1 == dup2
    assert math.isclose(math.sqrt(sum(v * v for v in dup1)), 1.0)
    assert cosine(dup1, related) > cosine(dup1, unrelated)


class FakeEmbeddingsAPI:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, *, input: list[str], model: str, dimensions: int) -> SimpleNamespace:
        self.calls.append({"n": len(input), "model": model, "dimensions": dimensions})
        # Return out of order to prove we sort by index.
        data = [
            SimpleNamespace(index=i, embedding=[float(len(text))] * dimensions)
            for i, text in reversed(list(enumerate(input)))
        ]
        return SimpleNamespace(data=data, usage=SimpleNamespace(total_tokens=len(input) * 3))


async def test_openai_embedder_batches_and_keeps_order() -> None:
    api = FakeEmbeddingsAPI()
    client = SimpleNamespace(embeddings=api)
    embedder = OpenAIEmbedder(client, "text-embedding-3-small", 4)  # type: ignore[arg-type]
    texts = [f"text {'x' * i}" for i in range(300)]

    vectors = await embedder.embed(texts)

    assert [call["n"] for call in api.calls] == [256, 44]
    assert api.calls[0] == {"n": 256, "model": "text-embedding-3-small", "dimensions": 4}
    assert [v[0] for v in vectors] == [float(len(t)) for t in texts]
    assert embedder.total_tokens == 900


def test_embedding_input_prefixes_breadcrumb() -> None:
    assert embedding_input("Indexes > Unique", "body") == "Indexes > Unique\n\nbody"
    assert embedding_input("", "body") == "body"
