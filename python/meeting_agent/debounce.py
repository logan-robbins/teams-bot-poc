"""
Debounced batching helper for the agent processing loop.

Real meetings produce bursts of speech turns ("ok... so... I think... we
should ship by Friday"). Naively firing one LLM call per final turn burns
money and produces redundant extractions. We coalesce: wait for the next
event, then keep draining as long as more events arrive within
``QUIET_WINDOW_SECONDS``, capped at ``MAX_BATCH``.

We return the *latest* event in the batch as the trigger. The session
ledger has all buffered events appended already — what the LLM sees on
the next run is the same regardless of which one we name as trigger.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TypeVar

logger = logging.getLogger(__name__)

DEFAULT_QUIET_WINDOW_SECONDS: float = 1.0
DEFAULT_MAX_BATCH: int = 6

T = TypeVar("T")


async def drain_with_debounce(
    queue: asyncio.Queue[T],
    *,
    quiet_window_seconds: float = DEFAULT_QUIET_WINDOW_SECONDS,
    max_batch: int = DEFAULT_MAX_BATCH,
) -> tuple[T, int]:
    """Wait for the next item, then coalesce followers within the quiet window.

    Returns ``(latest_item, batch_size)``. Caller can log if ``batch_size > 1``.
    """
    trigger = await queue.get()
    batch_size = 1
    while batch_size < max_batch:
        try:
            trigger = await asyncio.wait_for(queue.get(), timeout=quiet_window_seconds)
            batch_size += 1
        except asyncio.TimeoutError:
            break
    if batch_size > 1:
        logger.info("Debounced %d items into one tick", batch_size)
    return trigger, batch_size
