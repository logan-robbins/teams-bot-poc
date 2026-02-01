"""
Real-time Pub/Sub for Agent Thoughts

Provides an in-memory pub/sub system for streaming agent analysis
results to the Streamlit UI in real-time.

Uses asyncio queues for thread-safe communication between
the FastAPI backend and Streamlit frontend.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from dataclasses import dataclass, field
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class ThoughtType(str, Enum):
    """Types of agent thoughts published to the stream."""
    ANALYSIS = "analysis"           # Full analysis result
    OBSERVATION = "observation"      # Quick observation during processing
    ASSESSMENT = "assessment"        # Running assessment update
    SYSTEM = "system"               # System messages (session start/end)
    ERROR = "error"                 # Error messages


@dataclass
class AgentThought:
    """A single thought/update from the agent."""
    thought_type: ThoughtType
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    speaker_id: Optional[str] = None
    speaker_role: Optional[str] = None
    response_text: Optional[str] = None
    relevance_score: Optional[float] = None
    clarity_score: Optional[float] = None
    key_points: list[str] = field(default_factory=list)
    follow_up_suggestions: list[str] = field(default_factory=list)
    running_assessment: Optional[dict] = None
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
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
        """Convert to JSON string."""
        return json.dumps(self.to_dict())


class AgentThoughtPublisher:
    """
    Publisher for agent thoughts.
    
    Manages multiple subscriber queues and broadcasts thoughts to all.
    Thread-safe for use with asyncio.
    """
    
    def __init__(self, max_history: int = 100):
        self._subscribers: list[asyncio.Queue] = []
        self._history: list[AgentThought] = []
        self._max_history = max_history
        self._lock = asyncio.Lock()
        logger.info("AgentThoughtPublisher initialized")
    
    async def subscribe(self) -> asyncio.Queue:
        """
        Subscribe to agent thoughts.
        
        Returns an asyncio.Queue that will receive all published thoughts.
        Caller is responsible for calling unsubscribe when done.
        """
        queue: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            self._subscribers.append(queue)
            # Send history to new subscriber
            for thought in self._history:
                await queue.put(thought)
        logger.debug(f"New subscriber added. Total: {len(self._subscribers)}")
        return queue
    
    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a subscriber."""
        async with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)
        logger.debug(f"Subscriber removed. Total: {len(self._subscribers)}")
    
    async def publish(self, thought: AgentThought) -> None:
        """
        Publish a thought to all subscribers.
        
        Also stores in history for new subscribers.
        """
        async with self._lock:
            # Store in history
            self._history.append(thought)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]
            
            # Broadcast to all subscribers
            for queue in self._subscribers:
                try:
                    await queue.put(thought)
                except Exception as e:
                    logger.warning(f"Failed to publish to subscriber: {e}")
        
        logger.debug(f"Published thought: {thought.thought_type.value}")
    
    async def publish_analysis(
        self,
        content: str,
        speaker_id: Optional[str] = None,
        speaker_role: Optional[str] = None,
        response_text: Optional[str] = None,
        relevance_score: Optional[float] = None,
        clarity_score: Optional[float] = None,
        key_points: Optional[list[str]] = None,
        follow_up_suggestions: Optional[list[str]] = None,
        running_assessment: Optional[dict] = None,
    ) -> None:
        """Convenience method to publish an analysis thought."""
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
    
    async def publish_observation(self, content: str, speaker_id: Optional[str] = None) -> None:
        """Publish a quick observation."""
        thought = AgentThought(
            thought_type=ThoughtType.OBSERVATION,
            content=content,
            speaker_id=speaker_id,
        )
        await self.publish(thought)
    
    async def publish_assessment(self, content: str, assessment_data: dict) -> None:
        """Publish running assessment update."""
        thought = AgentThought(
            thought_type=ThoughtType.ASSESSMENT,
            content=content,
            running_assessment=assessment_data,
        )
        await self.publish(thought)
    
    async def publish_system(self, content: str) -> None:
        """Publish system message."""
        thought = AgentThought(
            thought_type=ThoughtType.SYSTEM,
            content=content,
        )
        await self.publish(thought)
    
    async def publish_error(self, content: str) -> None:
        """Publish error message."""
        thought = AgentThought(
            thought_type=ThoughtType.ERROR,
            content=content,
        )
        await self.publish(thought)
    
    def get_history(self) -> list[AgentThought]:
        """Get the thought history (for new subscribers)."""
        return list(self._history)
    
    def clear_history(self) -> None:
        """Clear the thought history."""
        self._history = []
    
    @property
    def subscriber_count(self) -> int:
        """Get the number of active subscribers."""
        return len(self._subscribers)


# Global publisher instance
_publisher: Optional[AgentThoughtPublisher] = None


def get_publisher() -> AgentThoughtPublisher:
    """Get the global publisher instance."""
    global _publisher
    if _publisher is None:
        _publisher = AgentThoughtPublisher()
    return _publisher
