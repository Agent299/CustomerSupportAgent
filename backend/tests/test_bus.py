from uuid import uuid4

import pytest

from shared.bus import InMemoryBus, KafkaBus
from shared.events import DLQ_SUFFIX, DeadLetter, RawDocumentEvent


def event(key: str) -> RawDocumentEvent:
    return RawDocumentEvent(version="9.0", key=key, op="UPSERT")


async def test_in_memory_bus_delivers_in_order_per_group() -> None:
    bus = InMemoryBus()
    for key in ("a", "b", "c"):
        await bus.publish("t", key, event(key))
    seen: list[str] = []

    async def handler(payload: str) -> None:
        seen.append(RawDocumentEvent.model_validate_json(payload).key)

    assert await bus.consume("t", "g1", handler) == 3
    assert await bus.consume("t", "g1", handler) == 0  # offsets committed
    assert await bus.consume("t", "g2", handler) == 3  # independent group
    assert seen == ["a", "b", "c", "a", "b", "c"]


async def test_failing_message_goes_to_dlq_and_does_not_block() -> None:
    bus = InMemoryBus(attempts=2)
    await bus.publish("t", "bad", event("bad"))
    await bus.publish("t", "good", event("good"))
    handled: list[str] = []

    async def handler(payload: str) -> None:
        key = RawDocumentEvent.model_validate_json(payload).key
        if key == "bad":
            raise ValueError("boom")
        handled.append(key)

    await bus.consume("t", "g", handler)

    assert handled == ["good"]
    [(key, payload)] = bus.topics["t" + DLQ_SUFFIX]
    letter = DeadLetter.model_validate_json(payload)
    assert key == "bad" and letter.topic == "t" and "boom" in letter.error


@pytest.mark.integration
async def test_kafka_bus_round_trip_and_dlq(kafka_servers: str) -> None:
    topic = f"test_{uuid4().hex[:8]}"
    bus = KafkaBus(kafka_servers, attempts=1)
    await bus.ensure_topics((topic,), partitions=1)
    try:
        await bus.publish(topic, "a", event("a"))
        await bus.publish(topic, "boom", event("boom"))
        seen: list[str] = []

        async def handler(payload: str) -> None:
            key = RawDocumentEvent.model_validate_json(payload).key
            if key == "boom":
                raise ValueError("boom")
            seen.append(key)

        assert await bus.consume(topic, "g", handler, idle_timeout_s=2) == 2
        assert seen == ["a"]
        # Committed: a second consumer in the same group sees nothing new.
        assert await bus.consume(topic, "g", handler, idle_timeout_s=2) == 0

        letters: list[str] = []

        async def collect(payload: str) -> None:
            letters.append(DeadLetter.model_validate_json(payload).key)

        await bus.consume(topic + DLQ_SUFFIX, "dlq-reader", collect, idle_timeout_s=2)
        assert letters == ["boom"]
    finally:
        await bus.close()
