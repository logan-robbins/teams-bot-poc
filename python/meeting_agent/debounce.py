"""
Debounced batching helper for the agent processing loop.

Real meetings produce bursts of speech turns ("ok... so... I think... we
should ship by Friday"). Naively firing one LLM call per final turn
burns money and produces redundant extractions. We coalesce.

Pipeline model
--------------
The agent loop is serial: pull a batch, run the LLM (which performs
any tool calls inside ``Runner.run``), persist, fan out to SSE, then
loop. The NEXT call to :func:`drain_with_debounce` only happens after
the previous LLM call returns. So we never start a tick before the
previous one finishes.

Two distinct waits matter
-------------------------
1. **Cold start** — the queue is empty. We block on the first event,
   then a short quiet window catches stragglers from a natural speech
   burst before firing.
2. **Backlog drain** — while the LLM was running, events accumulated.
   On loop re-entry we should fire ASAP using everything that has
   already arrived, NOT wait a second quiet window on top.

Before the fix, both paths used ``wait_for(quiet_window)`` which
added an artificial ~1s of latency after every LLM tick when there
was already a backlog. The drain-now-then-grace pattern below removes
that extra latency.

Returns the *latest* item in the batch as trigger. The session ledger
has all buffered events appended already — what the LLM sees on the
next run is the same regardless of which one we name as trigger.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TypeVar

logger = logging.getLogger(__name__)

# Cold-start grace window. Only applies when the queue is empty after
# pulling the first event — never charged on top of an existing backlog.
DEFAULT_QUIET_WINDOW_SECONDS: float = 0.5

# Soft cap on items per tick. With drain-now semantics this is mostly a
# safety bound for pathological backlogs (e.g. LLM stalls for 30s).
DEFAULT_MAX_BATCH: int = 32

T = TypeVar("T")


async def drain_with_debounce(
    queue: asyncio.Queue[T],
    *,
    quiet_window_seconds: float = DEFAULT_QUIET_WINDOW_SECONDS,
    max_batch: int = DEFAULT_MAX_BATCH,
) -> tuple[T, int]:
    """Pull next item, then drain whatever's already queued, then optional grace.

    Returns ``(latest_item, batch_size)``. Caller can log when
    ``batch_size > 1``.
    """
    trigger = await queue.get()
    batch_size = 1

    # Drain anything already in the queue with NO wait. This is the
    # "we just finished an LLM call, the speaker kept going" path.
    while batch_size < max_batch:
        try:
            trigger = queue.get_nowait()
            batch_size += 1
        except asyncio.QueueEmpty:
            break

    # If the backlog drained to empty before we hit max_batch, give a
    # short quiet window to catch stragglers from a natural pause-burst.
    # If the queue is non-empty here we already hit max_batch and won't
    # wait anyway.
    while batch_size < max_batch:
        try:
            trigger = await asyncio.wait_for(queue.get(), timeout=quiet_window_seconds)
            batch_size += 1
        except asyncio.TimeoutError:
            break

    if batch_size > 1:
        logger.info("Debounced %d items into one tick", batch_size)
    return trigger, batch_size
