"""Tests for the agent-loop debouncer."""

from __future__ import annotations

import asyncio

import pytest

from meeting_agent.debounce import drain_with_debounce
from meeting_agent.models import MeetingEvent


def _mk_event(idx: int) -> MeetingEvent:
    return MeetingEvent(
        event_id=f"speech:{idx}",
        kind="speech",
        source="teams_media",
        timestamp_utc=f"2026-04-22T16:00:{idx:02d}Z",
        text=f"turn {idx}",
    )


@pytest.mark.asyncio
async def test_returns_latest_when_burst_arrives_immediately() -> None:
    q: asyncio.Queue[MeetingEvent] = asyncio.Queue()
    await q.put(_mk_event(1))
    await q.put(_mk_event(2))
    await q.put(_mk_event(3))
    trigger, batch = await drain_with_debounce(q, quiet_window_seconds=0.05, max_batch=8)
    assert trigger.event_id == "speech:3"
    assert batch == 3


@pytest.mark.asyncio
async def test_caps_at_max_batch_and_leaves_remainder_enqueued() -> None:
    q: asyncio.Queue[MeetingEvent] = asyncio.Queue()
    for i in range(1, 6):
        await q.put(_mk_event(i))
    trigger, batch = await drain_with_debounce(q, quiet_window_seconds=0.05, max_batch=3)
    assert trigger.event_id == "speech:3"
    assert batch == 3
    assert q.qsize() == 2


@pytest.mark.asyncio
async def test_returns_first_when_silence_follows() -> None:
    q: asyncio.Queue[MeetingEvent] = asyncio.Queue()
    await q.put(_mk_event(7))
    trigger, batch = await drain_with_debounce(q, quiet_window_seconds=0.01, max_batch=8)
    assert trigger.event_id == "speech:7"
    assert batch == 1


@pytest.mark.asyncio
async def test_blocks_until_first_event_arrives() -> None:
    q: asyncio.Queue[MeetingEvent] = asyncio.Queue()

    async def producer() -> None:
        await asyncio.sleep(0.02)
        await q.put(_mk_event(99))

    asyncio.create_task(producer())
    trigger, batch = await drain_with_debounce(q, quiet_window_seconds=0.05, max_batch=8)
    assert trigger.event_id == "speech:99"
    assert batch == 1
