"""Per-session chat events on a Valkey stream, served to the widget as SSE (docs/PLAN.md §8.2).

A stream rather than pub/sub: events are kept (capped, with a TTL), so a client that
connects after the answer started, or reconnects with ``Last-Event-ID``, misses nothing.
Valkey holds nothing durable; the transcript lives in Postgres.
"""

import json
from collections.abc import AsyncIterator
from typing import Any, Literal

from valkey.asyncio import Valkey

EventType = Literal["ack", "token", "citations", "final", "escalated", "error"]

TERMINAL: frozenset[str] = frozenset({"final", "escalated", "error"})  # a turn's last event

MAX_EVENTS = 5000  # per session; a long answer is a few hundred token events
TTL_S = 24 * 3600


def stream_key(session_id: str) -> str:
    return f"chat:{session_id}:events"


class ChatEvents:
    """Needs a client created with ``decode_responses=True``."""

    def __init__(self, kv: Valkey) -> None:
        self._kv = kv

    async def publish(self, session_id: str, event: EventType, data: dict[str, Any]) -> str:
        key = stream_key(session_id)
        event_id: str = await self._kv.xadd(
            key, {"event": event, "data": json.dumps(data)}, maxlen=MAX_EVENTS, approximate=True
        )
        await self._kv.expire(key, TTL_S)
        return event_id

    async def last_id(self, session_id: str) -> str:
        """Id of the newest event, or "0" when there are none yet."""
        newest = await self._kv.xrevrange(stream_key(session_id), count=1)
        return str(newest[0][0]) if newest else "0"

    async def read(
        self, session_id: str, after: str = "$", *, block_ms: int = 15_000
    ) -> list[tuple[str, EventType, dict[str, Any]]]:
        """Events after ``after`` ("0" = from the start, "$" = only new ones). Blocks up to
        ``block_ms`` and returns [] on timeout."""
        response = await self._kv.xread({stream_key(session_id): after}, block=block_ms)
        return [
            (event_id, fields["event"], json.loads(fields["data"]))
            for _key, entries in response or []
            for event_id, fields in entries
        ]

    async def sse(self, session_id: str, after: str) -> AsyncIterator[str]:
        """Server-sent events after ``after`` until a turn ends (final, escalated, error).
        A comment line on each idle read keeps proxies from closing the connection."""
        cursor = after
        while True:
            events = await self.read(session_id, cursor)
            if not events:
                yield ": keep-alive\n\n"
                continue
            for event_id, event, data in events:
                cursor = event_id
                yield f"id: {event_id}\nevent: {event}\ndata: {json.dumps(data)}\n\n"
                if event in TERMINAL:
                    return
