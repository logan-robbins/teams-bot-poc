"""
Real-time Pub/Sub for Agent Thoughts.

Provides an in-memory pub/sub system for streaming agent analysis
results to the Streamlit UI in real-time.

Uses asyncio queues for thread-safe communication between
the FastAPI backend and Streamlit frontend.

Example usage:
    publisher = get_publisher()
    await publisher.publish_analysis(
        content="Candidate demonstrated strong problem-solving skills",
        speaker_id="speaker_0",
        relevance_score=0.85,
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


class ThoughtType(str, Enum):
    """
    Types of agent thoughts published to the stream.

    Attributes:
        ANALYSIS: Full analysis result from the agent.
        OBSERVATION: Quick observation during processing.
        ASSESSMENT: Running assessment update.
        SYSTEM: System messages (session start/end, etc.).
        ERROR: Error messages and exceptions.
    """

    ANALYSIS = "analysis"
    OBSERVATION = "observation"
    ASSESSMENT = "assessment"
    SYSTEM = "system"
    ERROR = "error"


def _get_utc_timestamp() -> str:
    """
    Get current UTC timestamp in ISO format.

    Returns:
        ISO-formatted UTC timestamp string.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class AgentThought:
    """
    A single thought/update from the agent.

    Represents a publishable event from the interview analysis agent,
    including analysis results, observations, and system messages.

    Attributes:
        thought_type: Category of the thought (analysis, observation, etc.).
        content: Main text content of the thought.
        timestamp: UTC timestamp when the thought was created.
        speaker_id: Optional speaker identifier.
        speaker_role: Optional role (candidate, interviewer).
        response_text: Original response text being analyzed.
        relevance_score: Score indicating relevance (0.0-1.0).
        clarity_score: Score indicating clarity (0.0-1.0).
        key_points: List of key points extracted from the response.
        follow_up_suggestions: Suggested follow-up questions.
        running_assessment: Current running assessment data.
    """

    thought_type: ThoughtType
    content: str
    timestamp: str = field(default_factory=_get_utc_timestamp)
    speaker_id: str | None = None
    speaker_role: str | None = None
    response_text: str | None = None
    relevance_score: float | None = None
    clarity_score: float | None = None
    key_points: list[str] = field(default_factory=list)
    follow_up_suggestions: list[str] = field(default_factory=list)
    running_assessment: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        """
        Convert thought to dictionary for JSON serialization.

        Returns:
            Dictionary representation of the thought.
        """
        return {
            "thought_type": self.thought_type.value,
            "content": self.content,
            "timestamp": self.timestamp,
            "speaker_id": self.speaker_id,
            "speaker_role": self.speaker_role,
            "response_text": self.response_text,
            "relevance_score": self.relevance_score,
            "clarity_score": self.clarity_score,
            "key_points": self.key_points,
            "follow_up_suggestions": self.follow_up_suggestions,
            "running_assessment": self.running_assessment,
        }

    def to_json(self) -> str:
        """
        Convert thought to JSON string.

        Returns:
            JSON string representation of the thought.
        """
        return json.dumps(self.to_dict())


class AgentThoughtPublisher:
    """
    Publisher for agent thoughts.

    Manages multiple subscriber queues and broadcasts thoughts to all.
    Thread-safe for use with asyncio through proper lock usage.

    Attributes:
        max_history: Maximum number of thoughts to retain in history.

    Example:
        publisher = AgentThoughtPublisher()

        # Subscribe to receive thoughts
        queue = await publisher.subscribe()

        # Publish a thought
        await publisher.publish_analysis(
            content="Analysis result",
            relevance_score=0.9,
        )

        # Receive the thought
        thought = await queue.get()
    """

    def __init__(self, max_history: int = 100) -> None:
        """
        Initialize the publisher.

        Args:
            max_history: Maximum number of thoughts to retain in history.
        """
        self._subscribers: list[asyncio.Queue[AgentThought]] = []
        self._history: list[AgentThought] = []
        self._max_history = max_history
        self._lock = asyncio.Lock()
        logger.info("AgentThoughtPublisher initialized with max_history=%d", max_history)

    async def subscribe(self) -> asyncio.Queue[AgentThought]:
        """
        Subscribe to agent thoughts.

        Returns an asyncio.Queue that will receive all published thoughts.
        Caller is responsible for calling unsubscribe when done.

        Returns:
            Queue that will receive published thoughts.
        """
        queue: asyncio.Queue[AgentThought] = asyncio.Queue()
        async with self._lock:
            self._subscribers.append(queue)
            # Send history to new subscriber
            for thought in self._history:
                await queue.put(thought)
        logger.debug("New subscriber added. Total: %d", len(self._subscribers))
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[AgentThought]) -> None:
        """
        Remove a subscriber.

        Args:
            queue: The queue to unsubscribe.
        """
        async with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)
        logger.debug("Subscriber removed. Total: %d", len(self._subscribers))

    async def publish(self, thought: AgentThought) -> None:
        """
        Publish a thought to all subscribers.

        Also stores in history for new subscribers.

        Args:
            thought: The thought to publish.
        """
        async with self._lock:
            # Store in history
            self._history.append(thought)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history :]

            # Broadcast to all subscribers
            for queue in self._subscribers:
                try:
                    await queue.put(thought)
                except Exception as e:
                    logger.warning("Failed to publish to subscriber: %s", e)

        logger.debug("Published thought: %s", thought.thought_type.value)

    async def publish_analysis(
        self,
        content: str,
        *,
        speaker_id: str | None = None,
        speaker_role: str | None = None,
        response_text: str | None = None,
        relevance_score: float | None = None,
        clarity_score: float | None = None,
        key_points: list[str] | None = None,
        follow_up_suggestions: list[str] | None = None,
        running_assessment: dict[str, object] | None = None,
    ) -> None:
        """
        Publish an analysis thought.

        Convenience method to publish a full analysis result.

        Args:
            content: Main analysis content/summary.
            speaker_id: Speaker identifier.
            speaker_role: Speaker role (candidate, interviewer).
            response_text: Original response text.
            relevance_score: Relevance score (0.0-1.0).
            clarity_score: Clarity score (0.0-1.0).
            key_points: Extracted key points.
            follow_up_suggestions: Suggested follow-up questions.
            running_assessment: Current assessment data.
        """
        thought = AgentThought(
            thought_type=ThoughtType.ANALYSIS,
            content=content,
            speaker_id=speaker_id,
            speaker_role=speaker_role,
            response_text=response_text,
            relevance_score=relevance_score,
            clarity_score=clarity_score,
            key_points=key_points or [],
            follow_up_suggestions=follow_up_suggestions or [],
            running_assessment=running_assessment,
        )
        await self.publish(thought)

    async def publish_observation(
        self,
        content: str,
        *,
        speaker_id: str | None = None,
    ) -> None:
        """
        Publish a quick observation.

        Args:
            content: Observation content.
            speaker_id: Optional speaker identifier.
        """
        thought = AgentThought(
            thought_type=ThoughtType.OBSERVATION,
            content=content,
            speaker_id=speaker_id,
        )
        await self.publish(thought)

    async def publish_assessment(
        self,
        content: str,
        assessment_data: dict[str, object],
    ) -> None:
        """
        Publish running assessment update.

        Args:
            content: Assessment summary.
            assessment_data: Detailed assessment data.
        """
        thought = AgentThought(
            thought_type=ThoughtType.ASSESSMENT,
            content=content,
            running_assessment=assessment_data,
        )
        await self.publish(thought)

    async def publish_system(self, content: str) -> None:
        """
        Publish system message.

        Args:
            content: System message content.
        """
        thought = AgentThought(
            thought_type=ThoughtType.SYSTEM,
            content=content,
        )
        await self.publish(thought)

    async def publish_error(self, content: str) -> None:
        """
        Publish error message.

        Args:
            content: Error message content.
        """
        thought = AgentThought(
            thought_type=ThoughtType.ERROR,
            content=content,
        )
        await self.publish(thought)

    async def get_history(self) -> list[AgentThought]:
        """
        Get the thought history (async-safe).

        Returns a copy of the history to prevent external modification.

        Returns:
            Copy of the thought history list.
        """
        async with self._lock:
            return list(self._history)

    async def clear_history(self) -> None:
        """
        Clear the thought history (async-safe).

        Removes all thoughts from history while holding the lock.
        """
        async with self._lock:
            self._history.clear()
        logger.debug("History cleared")

    @property
    def subscriber_count(self) -> int:
        """
        Get the number of active subscribers.

        Note: This is not async-safe and provides an approximate count.
        For exact count, use get_subscriber_count() instead.

        Returns:
            Approximate number of active subscribers.
        """
        return len(self._subscribers)

    async def get_subscriber_count(self) -> int:
        """
        Get the exact number of active subscribers (async-safe).

        Returns:
            Number of active subscribers.
        """
        async with self._lock:
            return len(self._subscribers)


# Global publisher instance
_publisher: AgentThoughtPublisher | None = None
_publisher_lock = asyncio.Lock()


def get_publisher() -> AgentThoughtPublisher:
    """
    Get the global publisher instance.

    Creates a new publisher if one doesn't exist. This function is
    synchronous for convenience but the publisher itself is async-safe.

    Returns:
        The global AgentThoughtPublisher instance.

    Note:
        For async contexts where you need guaranteed thread-safety during
        initialization, consider using get_publisher_async() instead.
    """
    global _publisher
    if _publisher is None:
        _publisher = AgentThoughtPublisher()
    return _publisher


async def get_publisher_async() -> AgentThoughtPublisher:
    """
    Get the global publisher instance with async-safe initialization.

    Creates a new publisher if one doesn't exist, using a lock to ensure
    thread-safe initialization in async contexts.

    Returns:
        The global AgentThoughtPublisher instance.
    """
    global _publisher
    if _publisher is None:
        async with _publisher_lock:
            # Double-check after acquiring lock
            if _publisher is None:
                _publisher = AgentThoughtPublisher()
    return _publisher


def reset_publisher() -> None:
    """
    Reset the global publisher instance.

    Primarily useful for testing to ensure a clean state between tests.
    """
    global _publisher
    _publisher = None
    logger.debug("Global publisher reset")
