"""Event bus: Kafka API (Redpanda) in deployments, in-memory for unit tests.

Delivery is at-least-once: an offset is committed only after the handler succeeds.
A message whose handler keeps failing goes to ``<topic>.dlq`` so one bad page cannot
block the partition (docs/PLAN.md §8.3).
"""

import asyncio
import contextlib
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Protocol

import structlog
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError
from pydantic import BaseModel

from shared.events import DLQ_SUFFIX, DeadLetter

log = structlog.get_logger()

Handler = Callable[[str], Awaitable[None]]  # receives the raw JSON payload


class EventBus(Protocol):
    async def publish(self, topic: str, key: str, event: BaseModel) -> None: ...

    async def consume(
        self,
        topic: str,
        group: str,
        handler: Handler,
        *,
        idle_timeout_s: float | None = 5.0,
    ) -> int:
        """Process messages until the topic is idle for ``idle_timeout_s`` (None = forever).
        Returns the number of messages handled (including dead-lettered ones)."""
        ...

    async def close(self) -> None: ...


async def _handle_with_retry(
    bus: EventBus, topic: str, key: str, payload: str, handler: Handler, attempts: int
) -> None:
    for attempt in range(1, attempts + 1):
        try:
            await handler(payload)
            return
        except Exception as exc:
            if attempt == attempts:
                log.exception("bus.dead_letter", topic=topic, key=key)
                letter = DeadLetter(topic=topic, key=key, payload=payload, error=repr(exc))
                await bus.publish(topic + DLQ_SUFFIX, key, letter)
                return
            await asyncio.sleep(0.2 * 2**attempt)


class InMemoryBus:
    def __init__(self, *, attempts: int = 3) -> None:
        self.topics: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self._offsets: dict[tuple[str, str], int] = defaultdict(int)
        self._attempts = attempts

    async def publish(self, topic: str, key: str, event: BaseModel) -> None:
        self.topics[topic].append((key, event.model_dump_json()))

    async def consume(
        self, topic: str, group: str, handler: Handler, *, idle_timeout_s: float | None = 5.0
    ) -> int:
        handled = 0
        while self._offsets[(topic, group)] < len(self.topics[topic]):
            key, payload = self.topics[topic][self._offsets[(topic, group)]]
            await _handle_with_retry(self, topic, key, payload, handler, self._attempts)
            self._offsets[(topic, group)] += 1
            handled += 1
        return handled

    async def close(self) -> None:
        return None


class KafkaBus:
    def __init__(self, bootstrap_servers: str, *, attempts: int = 3) -> None:
        self._servers = bootstrap_servers
        self._attempts = attempts
        self._producer: AIOKafkaProducer | None = None

    async def ensure_topics(self, topics: tuple[str, ...], partitions: int = 3) -> None:
        admin = AIOKafkaAdminClient(bootstrap_servers=self._servers)
        await admin.start()
        try:
            wanted = [NewTopic(t, num_partitions=partitions, replication_factor=1) for t in topics]
            wanted += [
                NewTopic(t + DLQ_SUFFIX, num_partitions=1, replication_factor=1) for t in topics
            ]
            existing = set(await admin.list_topics())
            missing = [t for t in wanted if t.name not in existing]
            if missing:
                with contextlib.suppress(TopicAlreadyExistsError):  # created concurrently
                    await admin.create_topics(missing)
        finally:
            await admin.close()

    async def _get_producer(self) -> AIOKafkaProducer:
        if self._producer is None:
            self._producer = AIOKafkaProducer(bootstrap_servers=self._servers, acks="all")
            await self._producer.start()
        return self._producer

    async def publish(self, topic: str, key: str, event: BaseModel) -> None:
        producer = await self._get_producer()
        await producer.send_and_wait(topic, event.model_dump_json().encode(), key=key.encode())

    async def consume(
        self, topic: str, group: str, handler: Handler, *, idle_timeout_s: float | None = 5.0
    ) -> int:
        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=self._servers,
            group_id=group,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        await consumer.start()
        handled = 0
        last_message_at = time.monotonic()
        try:
            while True:
                batches = await consumer.getmany(timeout_ms=1000, max_records=100)
                if not batches:
                    idle = time.monotonic() - last_message_at
                    if idle_timeout_s is not None and idle >= idle_timeout_s:
                        return handled
                    continue
                for records in batches.values():
                    for record in records:
                        key = record.key.decode() if record.key else ""
                        payload = record.value.decode() if record.value else ""
                        await _handle_with_retry(self, topic, key, payload, handler, self._attempts)
                        handled += 1
                await consumer.commit()
                last_message_at = time.monotonic()
        finally:
            await consumer.stop()

    async def close(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None
