"""
In-process event bus for live SSE fan-out.

One bus per sink process. Publishers call ``bus.publish(event_type, data)``;
subscribers open a SSE connection and consume via an async iterator.

Bounded per-subscriber queues guarantee that a slow client cannot inflate
memory indefinitely — on overflow we drop the subscriber rather than the
producer. In practice the browser consumer is trivial.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel

__all__ = [
    "AlfredEvent",
    "AlfredEventBus",
    "AlfredEventType",
    "detect_direct_address",
    "format_sse",
]

logger = logging.getLogger(__name__)

_DIRECT_ADDRESS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*alfred[\s,:!?]", re.IGNORECASE),
    re.compile(r"@alfred\b", re.IGNORECASE),
    re.compile(r"\bhey alfred\b", re.IGNORECASE),
    re.compile(r"\bok alfred\b", re.IGNORECASE),
    re.compile(r"\bexcuse me alfred\b", re.IGNORECASE),
]


def detect_direct_address(text: str) -> bool:
    """Return True if ``text`` is directly addressing Alfred by name."""
    return any(p.search(text) for p in _DIRECT_ADDRESS_PATTERNS)


AlfredEventType = Literal[
    "ledger_append",
    "extraction",
    "dossier_upsert",
    "tool_call",
    "session_state",
    "session_started",
    "session_ended",
    "heartbeat",
]


class AlfredEvent(BaseModel):
    """Envelope for one event published on the bus."""

    type: AlfredEventType
    session_id: str | None = None
    timestamp_utc: str
    data: dict[str, Any]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def format_sse(event: AlfredEvent) -> bytes:
    """Serialize an event to the ``text/event-stream`` wire format."""
    payload = json.dumps(event.data, ensure_ascii=False, default=str)
    parts = [
        f"event: {event.type}",
        f"data: {payload}",
        f"id: {event.timestamp_utc}",
        "",
        "",
    ]
    return "\n".join(parts).encode("utf-8")


class _Subscriber:
    __slots__ = ("queue", "session_filter", "id")

    def __init__(self, session_filter: str | None, queue_size: int, sub_id: int) -> None:
        self.queue: asyncio.Queue[AlfredEvent | None] = asyncio.Queue(maxsize=queue_size)
        self.session_filter = session_filter
        self.id = sub_id


class AlfredEventBus:
    """Tiny in-process pub/sub keyed by event type."""

    def __init__(self, subscriber_queue_size: int = 1024) -> None:
        self._subscribers: list[_Subscriber] = []
        self._lock = asyncio.Lock()
        self._queue_size = subscriber_queue_size
        self._next_id = 0

    async def publish(
        self,
        event_type: AlfredEventType,
        data: dict[str, Any] | BaseModel,
        session_id: str | None = None,
    ) -> None:
        if isinstance(data, BaseModel):
            payload = data.model_dump()
        else:
            payload = dict(data)

        event = AlfredEvent(
            type=event_type,
            session_id=session_id,
            timestamp_utc=_iso_now(),
            data=payload,
        )

        async with self._lock:
            subscribers = list(self._subscribers)

        dropped: list[_Subscriber] = []
        for sub in subscribers:
            if sub.session_filter and session_id and sub.session_filter != session_id:
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Dropping slow SSE subscriber %d", sub.id)
                dropped.append(sub)

        if dropped:
            async with self._lock:
                for sub in dropped:
                    if sub in self._subscribers:
                        self._subscribers.remove(sub)

    async def subscribe(self, session_filter: str | None = None) -> AsyncIterator[AlfredEvent]:
        """Yield published events until the consumer cancels."""
        async with self._lock:
            self._next_id += 1
            sub = _Subscriber(session_filter, self._queue_size, self._next_id)
            self._subscribers.append(sub)
        logger.info(
            "SSE subscriber %d attached (filter=%s, total=%d)",
            sub.id,
            session_filter,
            len(self._subscribers),
        )
        try:
            while True:
                event = await sub.queue.get()
                if event is None:
                    break
                yield event
        finally:
            async with self._lock:
                if sub in self._subscribers:
                    self._subscribers.remove(sub)
            logger.info("SSE subscriber %d detached", sub.id)

    async def close(self) -> None:
        """Signal all subscribers to drop their streams (used on shutdown)."""
        async with self._lock:
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for sub in subscribers:
            try:
                sub.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
