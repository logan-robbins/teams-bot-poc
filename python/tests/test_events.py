"""Tests for the in-process SSE event bus."""

from __future__ import annotations

import asyncio

import pytest

from meeting_agent.events import AlfredEventBus, format_sse
from meeting_agent.models import Decision


async def _collect(bus: AlfredEventBus, n: int, session_filter: str | None = None) -> list:
    received: list = []
    iterator = bus.subscribe(session_filter=session_filter)

    async def consume() -> None:
        async for event in iterator:
            received.append(event)
            if len(received) >= n:
                break

    task = asyncio.create_task(consume())
    # Give the subscribe coroutine a chance to register its subscriber.
    await asyncio.sleep(0)
    return received, task


@pytest.mark.asyncio
async def test_bus_fans_out_to_one_subscriber() -> None:
    bus = AlfredEventBus()
    received, task = await _collect(bus, 2)

    await bus.publish("ledger_append", {"text": "hi"}, session_id="s1")
    await bus.publish("extraction", {"notes": ["a"]}, session_id="s1")

    await asyncio.wait_for(task, timeout=1.0)
    assert [e.type for e in received] == ["ledger_append", "extraction"]
    assert received[0].session_id == "s1"
    assert received[1].data == {"notes": ["a"]}


@pytest.mark.asyncio
async def test_bus_fans_out_to_multiple_subscribers() -> None:
    bus = AlfredEventBus()
    r1, t1 = await _collect(bus, 1)
    r2, t2 = await _collect(bus, 1)

    await bus.publish("tool_call", {"tool_name": "send_to_meeting_chat"})

    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)
    assert len(r1) == 1 and len(r2) == 1
    assert r1[0].type == "tool_call"
    assert r2[0].type == "tool_call"


@pytest.mark.asyncio
async def test_bus_session_filter_excludes_other_sessions() -> None:
    bus = AlfredEventBus()
    received, task = await _collect(bus, 1, session_filter="s1")

    # These should NOT reach the s1 subscriber — it filters to s1.
    await bus.publish("ledger_append", {"x": 1}, session_id="s2")
    await bus.publish("ledger_append", {"x": 2}, session_id="s2")
    # This one should.
    await bus.publish("ledger_append", {"x": 3}, session_id="s1")

    await asyncio.wait_for(task, timeout=1.0)
    assert len(received) == 1
    assert received[0].data == {"x": 3}


@pytest.mark.asyncio
async def test_bus_accepts_pydantic_models_as_payload() -> None:
    bus = AlfredEventBus()
    received, task = await _collect(bus, 1)
    await bus.publish(
        "dossier_upsert",
        Decision(id="d1", text="ship it", status="committed"),
    )
    await asyncio.wait_for(task, timeout=1.0)
    assert received[0].data["id"] == "d1"
    assert received[0].data["status"] == "committed"


@pytest.mark.asyncio
async def test_bus_close_drains_subscribers() -> None:
    bus = AlfredEventBus()

    async def consume() -> int:
        count = 0
        async for _ in bus.subscribe():
            count += 1
        return count

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await bus.publish("heartbeat", {})
    await asyncio.sleep(0.01)
    await bus.close()
    total = await asyncio.wait_for(task, timeout=1.0)
    assert total == 1


def test_format_sse_frames_correctly() -> None:
    from meeting_agent.events import AlfredEvent

    event = AlfredEvent(
        type="heartbeat",
        session_id="s1",
        timestamp_utc="2026-04-22T16:00:00Z",
        data={"ok": True},
    )
    frame = format_sse(event).decode("utf-8")
    assert frame.startswith("event: heartbeat\n")
    assert 'data: {"ok": true}' in frame
    assert frame.endswith("\n\n")
