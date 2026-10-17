from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.main import create_app
from shared.db.models import IngestOutbox

pytestmark = pytest.mark.integration

TOKEN = "test-ingest-token"  # noqa: S105 - test fixture value
DOC = {
    "source_id": "DOC-1",
    "url": "https://www.mongodb.com/docs/v7.0/core/index-unique/",
    "type": "DOC",
}


@pytest.fixture
def client(database_url: str, sessions: async_sessionmaker[AsyncSession]) -> Iterator[TestClient]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, database_url=database_url, ingest_webhook_token=TOKEN
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


async def outbox_payloads(sessions: async_sessionmaker[AsyncSession]) -> list[dict[str, object]]:
    async with sessions() as session:
        return [row.payload for row in await session.scalars(select(IngestOutbox))]


async def test_valid_request_is_queued_in_outbox(
    client: TestClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    response = client.post("/api/ingest/collect", json=DOC, headers={"X-Ingest-Token": TOKEN})

    assert response.status_code == 202
    assert response.json()["status"] == "QUEUED"
    [payload] = await outbox_payloads(sessions)
    assert payload["version"] == "7.0"
    assert payload["key"] == "core/index-unique"
    assert payload["op"] == "UPSERT"


@pytest.mark.parametrize("headers", [{}, {"X-Ingest-Token": "wrong"}])
async def test_missing_or_wrong_token_is_rejected(
    client: TestClient, sessions: async_sessionmaker[AsyncSession], headers: dict[str, str]
) -> None:
    response = client.post("/api/ingest/collect", json=DOC, headers=headers)

    assert response.status_code == 401
    assert await outbox_payloads(sessions) == []


@pytest.mark.parametrize(
    "body",
    [
        {**DOC, "url": "https://www.mongodb.com/docs/atlas/whatever/"},
        {**DOC, "url": "https://example.com/docs/manual/x/"},
        {**DOC, "type": "TICKET"},
    ],
)
def test_unsupported_sources_are_unprocessable(client: TestClient, body: dict[str, str]) -> None:
    response = client.post("/api/ingest/collect", json=body, headers={"X-Ingest-Token": TOKEN})

    assert response.status_code == 422


def test_endpoint_hidden_when_no_token_configured(database_url: str) -> None:
    settings = Settings(_env_file=None, database_url=database_url)  # type: ignore[call-arg]
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/ingest/collect", json=DOC, headers={"X-Ingest-Token": "x"})

    assert response.status_code == 404


def test_blank_configured_token_keeps_endpoint_disabled(database_url: str) -> None:
    settings = Settings(_env_file=None, database_url=database_url, ingest_webhook_token="")  # type: ignore[call-arg]
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/ingest/collect", json=DOC, headers={"X-Ingest-Token": ""})

    assert response.status_code == 404
