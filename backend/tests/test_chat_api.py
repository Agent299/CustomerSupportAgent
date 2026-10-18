"""Chat API end to end over HTTP: Postgres + Valkey real, search and model faked."""

import asyncio
import json
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from valkey.asyncio import Valkey

from app.config import Settings
from app.main import create_app
from app.orchestrator.pipeline import QueryDeps
from app.orchestrator.prompts import FALLBACK
from shared.db.models import Feedback, LlmUsageDaily, Ticket
from tests.fakes import FakeEmbedder, FakeIndex, FakeLlm, hit

pytestmark = pytest.mark.integration

CHUNK = "a" * 16
API = "/api/v1/chat"


class Stack:
    """The fake search + model the app gets for each turn; tests swap ``llm``."""

    def __init__(self) -> None:
        self.llm = FakeLlm(f"A unique index rejects duplicate values [c:{CHUNK}].")
        self.index = FakeIndex([hit(CHUNK, "core/index-unique")])

    def deps(self) -> QueryDeps:
        return QueryDeps(self.index, FakeEmbedder(), self.llm)  # type: ignore[arg-type]


@pytest.fixture
def stack() -> Stack:
    return Stack()


@pytest.fixture
def client(
    database_url: str,
    valkey_url: str,
    sessions: async_sessionmaker[AsyncSession],
    stack: Stack,
) -> Iterator[TestClient]:
    async def flush() -> None:
        kv = Valkey.from_url(valkey_url)
        await kv.flushdb()
        await kv.aclose()

    asyncio.run(flush())
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, database_url=database_url, redis_url=valkey_url, daily_spend_cap_usd=1.0
    )
    with TestClient(create_app(settings, query_deps=stack.deps)) as test_client:
        yield test_client


def new_session(client: TestClient, **body: Any) -> tuple[str, dict[str, str]]:
    response = client.post(f"{API}/sessions", json=body or None)
    assert response.status_code == 201
    data = response.json()
    return data["session_id"], {"X-Session-Token": data["session_token"]}


def read_turn(
    client: TestClient, sid: str, headers: dict[str, str], after: str
) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    with client.stream(
        "GET", f"{API}/sessions/{sid}/stream?after={after}", headers=headers
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        event = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                events.append((str(event), json.loads(line.removeprefix("data: "))))
    return events


def turn(client: TestClient, sid: str, headers: dict[str, str], text: str) -> list[tuple[str, Any]]:
    response = client.post(f"{API}/sessions/{sid}/messages", json={"text": text}, headers=headers)
    assert response.status_code == 202, response.text
    return read_turn(client, sid, headers, response.json()["stream_after"])


async def ticket(sessions: async_sessionmaker[AsyncSession], sid: str) -> Ticket:
    async with sessions() as session:
        [row] = (await session.scalars(select(Ticket))).all()
        assert str(row.id) == sid
        return row


async def test_answered_turn_streams_tokens_then_final_and_persists(
    client: TestClient, sessions: async_sessionmaker[AsyncSession], stack: Stack
) -> None:
    sid, headers = new_session(client)

    events = turn(client, sid, headers, "Why E11000? my email is jane@corp.com")

    kinds = [kind for kind, _ in events]
    assert kinds[0] == "ack" and kinds[-2:] == ["citations", "final"]
    assert "token" in kinds
    final = events[-1][1]
    streamed = "".join(data["text"] for kind, data in events if kind == "token").strip()
    assert final["answer"] == streamed
    assert final["route"] == "ANSWERED" and final["resolved_version"] == "9.0"
    assert final["citations"][0]["url"].endswith("/docs/manual/core/index-unique/")
    # PII is masked before storage and before the model sees it
    sent_to_model = stack.llm.calls[-1][1][-1]["content"]
    assert "jane@corp.com" not in sent_to_model and "<EMAIL_MASKED>" in sent_to_model
    transcript = client.get(f"{API}/sessions/{sid}", headers=headers).json()
    assert [m["role"] for m in transcript["messages"]] == ["user", "bot"]
    assert "<EMAIL_MASKED>" in transcript["messages"][0]["content"]
    assert transcript["status"] == "BOT_ACTIVE"


async def test_second_turn_sends_history_and_session_version(
    client: TestClient, stack: Stack
) -> None:
    sid, headers = new_session(client, version_hint="7.0")
    turn(client, sid, headers, "How do unique indexes work?")

    events = turn(client, sid, headers, "And on a sharded collection?")

    assert events[-1][1]["resolved_version"] == "7.0"
    assert stack.index.queries[-1][1] == ["7.0"]
    answer_messages = stack.llm.calls[-1][1]
    assert [m["role"] for m in answer_messages[1:3]] == ["user", "assistant"]
    assert [task for task, _ in stack.llm.calls].count("rewrite") == 1


async def test_rule_escalation_then_bot_stays_out(
    client: TestClient, sessions: async_sessionmaker[AsyncSession], stack: Stack
) -> None:
    sid, headers = new_session(client)

    first = turn(client, sid, headers, "I was charged twice, I want a refund")
    second = turn(client, sid, headers, "Hello? Anyone?")

    assert [k for k, _ in first] == ["ack", "escalated"]
    assert first[-1][1]["reason"] == "billing"
    assert (await ticket(sessions, sid)).status == "ESCALATED"
    assert [k for k, _ in second] == ["ack", "escalated"]
    assert "support engineer" in second[-1][1]["message"]
    assert stack.llm.calls == []


@pytest.mark.parametrize(
    ("answer", "reason"),
    [(FALLBACK, "out_of_scope"), (f"Made up [c:{'f' * 16}].", "invalid_citation")],
)
async def test_unsupported_answer_is_replaced_by_handoff(
    client: TestClient,
    sessions: async_sessionmaker[AsyncSession],
    stack: Stack,
    answer: str,
    reason: str,
) -> None:
    stack.llm = FakeLlm(answer)
    sid, headers = new_session(client)

    events = turn(client, sid, headers, "How do I tune nginx?")

    assert "token" in [k for k, _ in events]  # streamed before validation...
    kind, data = events[-1]
    assert (kind, data["reason"]) == ("escalated", reason)  # ...then replaced
    transcript = client.get(f"{API}/sessions/{sid}", headers=headers).json()
    assert transcript["messages"][-1]["content"] == data["message"]
    assert transcript["status"] == "ESCALATED"


async def test_spend_is_metered_and_the_cap_stops_answers(
    client: TestClient, sessions: async_sessionmaker[AsyncSession], stack: Stack
) -> None:
    stack.llm = FakeLlm(f"ok [c:{CHUNK}]", cost_usd=0.6)
    sid, headers = new_session(client)

    turn(client, sid, headers, "first")
    turn(client, sid, headers, "second")  # 1.20 spent, cap is 1.00
    calls_before = len(stack.llm.calls)
    events = turn(client, sid, headers, "third")

    assert events[-1] == ("error", events[-1][1]) and events[-1][1]["code"] == "spend_cap"
    assert len(stack.llm.calls) == calls_before
    async with sessions() as session:
        [usage] = (await session.scalars(select(LlmUsageDaily))).all()
    assert usage.cost_usd == Decimal("1.2")


async def test_wrong_token_and_unknown_session_are_404(client: TestClient) -> None:
    sid, _ = new_session(client)
    other = {"X-Session-Token": "x" * 43}

    assert (
        client.post(
            f"{API}/sessions/{sid}/messages", json={"text": "hi"}, headers=other
        ).status_code
        == 404
    )
    assert client.get(f"{API}/sessions/{sid}", headers=other).status_code == 404
    assert client.get(f"{API}/sessions/{sid}/stream", headers=other).status_code == 404
    unknown = "00000000-0000-4000-8000-000000000000"
    assert client.get(f"{API}/sessions/{unknown}", headers=other).status_code == 404
    assert client.get(f"{API}/sessions/{sid}").status_code == 422  # header missing


async def test_one_turn_at_a_time(client: TestClient, valkey_url: str) -> None:
    sid, headers = new_session(client)

    async def hold_lock() -> None:
        kv = Valkey.from_url(valkey_url)
        await kv.set(f"turn:{sid}", "1", ex=30)
        await kv.aclose()

    await hold_lock()
    response = client.post(f"{API}/sessions/{sid}/messages", json={"text": "hi"}, headers=headers)

    assert response.status_code == 409


async def test_feedback_on_bot_messages_only_and_upserts(
    client: TestClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    sid, headers = new_session(client)
    final = turn(client, sid, headers, "question")[-1][1]
    user_message = client.get(f"{API}/sessions/{sid}", headers=headers).json()["messages"][0]

    url = f"{API}/messages/{final['message_id']}/feedback"
    assert (
        client.post(
            url, json={"rating": -1, "comment": "call me 555-123-4567"}, headers=headers
        ).status_code
        == 204
    )
    assert client.post(url, json={"rating": 1}, headers=headers).status_code == 204
    assert (
        client.post(
            f"{API}/messages/{user_message['id']}/feedback", json={"rating": 1}, headers=headers
        ).status_code
        == 404
    )
    async with sessions() as session:
        [row] = (await session.scalars(select(Feedback))).all()
    assert (row.rating, row.comment) == (1, None)


async def test_escalate_on_request(
    client: TestClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    sid, headers = new_session(client)

    turn(client, sid, headers, "How do unique indexes work?")

    response = client.post(f"{API}/sessions/{sid}/escalate", headers=headers)
    events = read_turn(client, sid, headers, response.json()["stream_after"])

    assert response.status_code == 202
    assert [kind for kind, _ in events] == ["escalated"]  # only this action's event
    assert events[0][1]["reason"] == "human_request"
    async with sessions() as session:
        row = await session.get(Ticket, uuid.UUID(sid))
    assert row is not None and (row.status, row.escalation_reason) == ("ESCALATED", "human_request")


def test_chat_is_503_without_models(database_url: str, valkey_url: str) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, database_url=database_url, redis_url=valkey_url, openai_api_key=None
    )
    with TestClient(create_app(settings)) as test_client:
        assert test_client.post(f"{API}/sessions").status_code == 503
