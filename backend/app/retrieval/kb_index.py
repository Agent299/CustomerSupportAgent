"""OpenSearch knowledge-base index: hybrid BM25 + kNN (docs/PLAN.md §8.4, ADR 0003).

``kb_chunks`` is an alias over a versioned physical index (``kb_chunks_v1``) so the mapping
can change later via reindex + alias swap without downtime.
"""

import contextlib
from dataclasses import dataclass
from typing import Any

from opensearchpy import AsyncOpenSearch, NotFoundError
from opensearchpy.helpers import async_bulk

from app.config import Settings

SCHEMA_VERSION = 1
HYBRID_PIPELINE = "kb-hybrid"
# Lexical vs semantic weight after min-max normalization; tuned on the golden set (Phase 2).
HYBRID_WEIGHTS = (0.3, 0.7)


def make_client(settings: Settings) -> AsyncOpenSearch:
    auth = None
    if settings.opensearch_user and settings.opensearch_password:
        auth = (settings.opensearch_user, settings.opensearch_password.get_secret_value())
    return AsyncOpenSearch(hosts=[settings.opensearch_url], http_auth=auth, timeout=30)


def index_body(dimensions: int) -> dict[str, Any]:
    keyword = {"type": "keyword"}
    return {
        "settings": {"index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0}},
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "chunk_id": keyword,
                "text": {"type": "text", "analyzer": "english"},
                "breadcrumb": {"type": "text", "analyzer": "english"},
                "titles": {"type": "text", "analyzer": "english"},
                "doc_keys": keyword,
                "versions": keyword,
                "source_type": keyword,
                "security_level": keyword,
                "token_count": {"type": "integer"},
                "updated_at": {"type": "date"},
                "embedding_model": keyword,
                "embedding": {
                    "type": "knn_vector",
                    "dimension": dimensions,
                    # Lucene HNSW supports cosine and efficient filtered kNN (version filter).
                    "method": {
                        "name": "hnsw",
                        "engine": "lucene",
                        "space_type": "cosinesimil",
                        "parameters": {"m": 16, "ef_construction": 128},
                    },
                },
            },
        },
    }


@dataclass(frozen=True)
class Hit:
    chunk_id: str
    score: float
    source: dict[str, Any]


class KbIndex:
    def __init__(self, client: AsyncOpenSearch, alias: str, dimensions: int) -> None:
        self._client = client
        self.alias = alias
        self.dimensions = dimensions

    @property
    def physical_name(self) -> str:
        return f"{self.alias}_v{SCHEMA_VERSION}"

    async def ensure(self) -> None:
        """Create index, alias and hybrid search pipeline if missing (idempotent)."""
        if not await self._client.indices.exists(index=self.physical_name):
            await self._client.indices.create(
                index=self.physical_name, body=index_body(self.dimensions)
            )
        if not await self._client.indices.exists_alias(name=self.alias):
            await self._client.indices.put_alias(index=self.physical_name, name=self.alias)
        await self._client.search_pipeline.put(
            id=HYBRID_PIPELINE,
            body={
                "description": "min-max normalize BM25 and kNN scores, then weighted mean",
                "phase_results_processors": [
                    {
                        "normalization-processor": {
                            "normalization": {"technique": "min_max"},
                            "combination": {
                                "technique": "arithmetic_mean",
                                "parameters": {"weights": list(HYBRID_WEIGHTS)},
                            },
                        }
                    }
                ],
            },
        )

    async def drop(self) -> None:
        with contextlib.suppress(NotFoundError):
            await self._client.indices.delete(index=self.physical_name)

    async def upsert(self, documents: list[dict[str, Any]]) -> None:
        actions = (
            {"_op_type": "index", "_index": self.alias, "_id": doc["chunk_id"], "_source": doc}
            for doc in documents
        )
        await async_bulk(self._client, actions, refresh="wait_for")

    async def update_metadata(self, updates: list[dict[str, Any]]) -> list[str]:
        """Partial update (no re-embedding). Returns ids missing from the index."""
        actions = (
            {"_op_type": "update", "_index": self.alias, "_id": doc["chunk_id"], "doc": doc}
            for doc in updates
        )
        _, errors = await async_bulk(
            self._client, actions, refresh="wait_for", raise_on_error=False
        )
        missing: list[str] = []
        for error in errors if isinstance(errors, list) else []:
            item = error.get("update", {})
            if item.get("status") != 404:
                raise RuntimeError(f"OpenSearch update failed: {item}")
            missing.append(str(item["_id"]))
        return missing

    async def delete(self, chunk_ids: list[str]) -> None:
        actions = (
            {"_op_type": "delete", "_index": self.alias, "_id": chunk_id} for chunk_id in chunk_ids
        )
        await async_bulk(self._client, actions, refresh="wait_for", ignore_status=(404,))

    async def get(self, chunk_id: str) -> dict[str, Any] | None:
        try:
            response = await self._client.get(index=self.alias, id=chunk_id)
        except NotFoundError:
            return None
        source: dict[str, Any] = response["_source"]
        return source

    async def count(self) -> int:
        response = await self._client.count(index=self.alias)
        return int(response["count"])

    async def hybrid_search(
        self,
        query: str,
        vector: list[float],
        *,
        versions: list[str] | None = None,
        k: int = 40,
    ) -> list[Hit]:
        filters: list[dict[str, Any]] = [{"term": {"security_level": "PUBLIC"}}]
        if versions:
            filters.append({"terms": {"versions": versions}})
        body = {
            "size": k,
            "_source": {"excludes": ["embedding"]},
            "query": {
                "hybrid": {
                    "queries": [
                        {
                            "bool": {
                                "must": {
                                    "multi_match": {
                                        "query": query,
                                        "fields": ["text", "breadcrumb^2", "titles^2"],
                                    }
                                },
                                "filter": filters,
                            }
                        },
                        {
                            "knn": {
                                "embedding": {
                                    "vector": vector,
                                    "k": k,
                                    "filter": {"bool": {"filter": filters}},
                                }
                            }
                        },
                    ]
                }
            },
        }
        response = await self._client.search(
            index=self.alias, body=body, params={"search_pipeline": HYBRID_PIPELINE}
        )
        return [
            Hit(chunk_id=hit["_id"], score=float(hit["_score"]), source=hit["_source"])
            for hit in response["hits"]["hits"]
        ]
