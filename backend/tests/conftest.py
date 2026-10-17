import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.db.session import make_engine, make_session_factory

BACKEND_DIR = Path(__file__).resolve().parents[1]
ADMIN_DSN = os.environ.get("TEST_ADMIN_DSN", "postgresql://support:support@localhost:5432/support")
TEST_DB = "support_test"
TEST_DATABASE_URL = (
    ADMIN_DSN.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://") + f"/{TEST_DB}"
)
IN_CI = os.environ.get("CI") == "true" or os.environ.get("APP_ENV") == "ci"


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
    """A migrated test database. Skips locally if Postgres is down; fails in CI."""
    try:
        asyncio.run(_ensure_test_database())
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        if IN_CI:
            raise
        pytest.skip(f"Postgres not reachable ({exc}); run `make up` for integration tests")
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
            text("TRUNCATE chunk_occurrences, chunks, documents, ingest_runs RESTART IDENTITY")
        )
    yield make_session_factory(engine)
    await engine.dispose()
