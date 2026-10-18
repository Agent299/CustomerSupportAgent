"""Integration fixtures. A backing service that is down skips its tests locally (run
`make up`) but fails them in CI, where every service is provided."""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
from aiokafka.admin import AIOKafkaAdminClient
from opensearchpy import AsyncOpenSearch
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from valkey.asyncio import Valkey

from app.retrieval.kb_index import KbIndex
from shared.db.session import make_engine, make_session_factory

BACKEND_DIR = Path(__file__).resolve().parents[1]
ADMIN_DSN = os.environ.get("TEST_ADMIN_DSN", "postgresql://support:support@localhost:5432/support")
TEST_DB = "support_test"
TEST_DATABASE_URL = (
    ADMIN_DSN.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://") + f"/{TEST_DB}"
)
OPENSEARCH_URL = os.environ.get("TEST_OPENSEARCH_URL", "http://localhost:9200")
KAFKA_SERVERS = os.environ.get("TEST_KAFKA_SERVERS", "localhost:19092")
VALKEY_URL = os.environ.get("TEST_VALKEY_URL", "redis://localhost:6379/15")  # db 15: tests only
IN_CI = os.environ.get("CI") == "true" or os.environ.get("APP_ENV") == "ci"
TEST_DIMENSIONS = 64


def _unavailable(service: str, exc: BaseException) -> None:
    if IN_CI:
        raise RuntimeError(f"{service} must be available in CI") from exc
    pytest.skip(f"{service} not reachable ({exc!r}); run `make up` for integration tests")


async def _ensure_test_database() -> None:
    conn = await asyncpg.connect(ADMIN_DSN, timeout=3)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """A migrated test database."""
    try:
        asyncio.run(_ensure_test_database())
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        _unavailable("Postgres", exc)
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        check=True,
        capture_output=True,
    )
    yield TEST_DATABASE_URL


@pytest.fixture
async def sessions(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(database_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE chunk_occurrences, chunks, documents, ingest_runs, ingest_outbox, "
                "feedback, messages, tickets, llm_usage_daily, identifier_anchors RESTART IDENTITY"
            )
        )
    yield make_session_factory(engine)
    await engine.dispose()


@pytest.fixture(scope="session")
def opensearch_url() -> str:
    try:
        httpx.get(OPENSEARCH_URL, timeout=3).raise_for_status()
    except httpx.HTTPError as exc:
        _unavailable("OpenSearch", exc)
    return OPENSEARCH_URL


@pytest.fixture
async def kb(opensearch_url: str) -> AsyncIterator[KbIndex]:
    """A fresh, uniquely named index per test."""
    client = AsyncOpenSearch(hosts=[opensearch_url], timeout=30)
    index = KbIndex(client, f"test_kb_{uuid4().hex[:8]}", TEST_DIMENSIONS)
    await index.ensure()
    yield index
    await index.drop()
    await client.close()


@pytest.fixture(scope="session")
def kafka_servers() -> str:
    async def probe() -> None:
        admin = AIOKafkaAdminClient(bootstrap_servers=KAFKA_SERVERS, request_timeout_ms=3000)
        await admin.start()
        await admin.close()

    try:
        asyncio.run(asyncio.wait_for(probe(), timeout=10))
    except Exception as exc:
        _unavailable("Kafka/Redpanda", exc)
    return KAFKA_SERVERS


@pytest.fixture(scope="session")
def valkey_url() -> str:
    async def probe() -> None:
        client = Valkey.from_url(VALKEY_URL)
        try:
            await client.ping()
        finally:
            await client.aclose()

    try:
        asyncio.run(asyncio.wait_for(probe(), timeout=5))
    except Exception as exc:
        _unavailable("Valkey", exc)
    return VALKEY_URL
