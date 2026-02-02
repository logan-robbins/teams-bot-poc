"""
Interview Simulation Engine for Streamlit Integration

Provides a stateful simulation engine that plays back the INTERVIEW_SCRIPT
with realistic timing, pause/resume support, and transcript sink integration.

Usage:
    from simulation_engine import SimulationEngine

    engine = SimulationEngine()
    await engine.start()
    # ... Streamlit polling loop ...
    await engine.stop()  # Pause
    await engine.start()  # Resume
    await engine.restart()  # Start fresh
"""

import asyncio
import httpx
import random
import threading
import logging
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field
from enum import Enum

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

SINK_URL = "http://127.0.0.1:8765"
CANDIDATE_NAME = "Sarah Chen"
MEETING_URL = "https://teams.microsoft.com/l/meetup-join/simulated-interview-session"

# Speaker IDs (matching diarization format)
INTERVIEWER_ID = "speaker_0"
CANDIDATE_ID = "speaker_1"

# Timing configuration (seconds)
INTERVIEWER_DELAY_MIN = 2.0
INTERVIEWER_DELAY_MAX = 3.0
CANDIDATE_DELAY_MIN = 3.0
CANDIDATE_DELAY_MAX = 5.0
CANDIDATE_WORDS_PER_SECOND = 2.5  # For length-based delay calculation


# =============================================================================
# Interview Script (20 messages)
# =============================================================================

INTERVIEW_SCRIPT = [
    # Message 1-2: Opening
    (INTERVIEWER_ID, "Good morning Sarah, thanks for joining us today. I'm David, the Engineering Manager. Before we dive in, how are you doing today?"),
    (CANDIDATE_ID, "Good morning David! I'm doing great, thank you for asking. I'm really excited about this opportunity and looking forward to our conversation."),
    
    # Message 3-4: Background
    (INTERVIEWER_ID, "Wonderful. Let's start with your background. Can you walk me through your experience with Python and tell me about a project you're particularly proud of?"),
    (CANDIDATE_ID, "Absolutely. I've been working with Python for about six years now, primarily in backend development. The project I'm most proud of is a real-time data pipeline I built at my current company. We were processing clickstream data from our e-commerce platform, handling about 50,000 events per second. I designed the architecture using Apache Kafka for ingestion and built custom consumers in Python with asyncio. The system reduced our data latency from hours to under 30 seconds."),
    
    # Message 5-6: Technical deep dive
    (INTERVIEWER_ID, "That's impressive throughput. How did you handle failures and ensure data consistency in that pipeline?"),
    (CANDIDATE_ID, "Great question. We implemented several layers of reliability. First, Kafka's built-in replication handled broker failures. For our consumers, I used idempotent processing with deduplication based on event IDs stored in Redis. We also implemented dead letter queues for messages that failed processing after three retries. For monitoring, I set up Prometheus metrics and PagerDuty alerts for consumer lag and error rates. We achieved 99.97% data delivery reliability."),
    
    # Message 7-8: System design
    (INTERVIEWER_ID, "Nice. Let's shift to system design. If you were tasked with building a real-time collaborative document editor like Google Docs, how would you approach it?"),
    (CANDIDATE_ID, "I'd start by identifying the core challenges: real-time synchronization, conflict resolution, and scalability. For the sync layer, I'd use WebSockets with a message broker like Redis Pub/Sub for horizontal scaling. The key technical challenge is handling concurrent edits. I'd implement Operational Transformation or CRDTs, probably CRDTs since they're more mathematically sound for eventual consistency. For storage, I'd use a combination of PostgreSQL for document metadata and a specialized data structure for the document content itself. I'd also implement presence awareness so users can see who else is editing."),
    
    # Message 9-10: Debugging scenario
    (INTERVIEWER_ID, "Good approach. Now, imagine you're on call and get paged at 3 AM because the document editor is showing 10 second delays. Walk me through your debugging process."),
    (CANDIDATE_ID, "First, I'd check our monitoring dashboards to understand the scope. Is it all users or specific regions? Then I'd look at key metrics: WebSocket connection counts, message queue depth, database query latency, and CPU/memory on our servers. If the queue depth is high, we have a consumer bottleneck. If database latency spiked, I'd check for slow queries or locks. I'd also verify recent deployments. Once I identify the bottleneck, I'd either scale horizontally if it's capacity, rollback if it's a bad deploy, or implement a quick mitigation like rate limiting while we fix the root cause. Communication is key too. I'd update the status page and keep stakeholders informed."),
    
    # Message 11-12: Code quality
    (INTERVIEWER_ID, "Good systematic approach. How do you ensure code quality in your projects? What's your testing philosophy?"),
    (CANDIDATE_ID, "I follow the testing pyramid: lots of unit tests, fewer integration tests, and minimal end-to-end tests. For Python, I use pytest religiously. I aim for high coverage on business logic but don't obsess over 100% coverage. I also write property-based tests with Hypothesis for edge case discovery. Beyond testing, I enforce type hints with mypy in strict mode and use ruff for linting. Code reviews are crucial too. I believe every PR should be reviewed, and I try to give thorough, constructive feedback. Documentation is often overlooked but I make sure complex functions have docstrings explaining the why, not just the what."),
    
    # Message 13-14: Team collaboration
    (INTERVIEWER_ID, "Speaking of code reviews, tell me about a time you disagreed with a colleague on a technical decision. How did you handle it?"),
    (CANDIDATE_ID, "Last year, we had a heated debate about microservices versus keeping our monolith. My colleague wanted to break everything into services immediately. I was concerned about the operational complexity and believed we should be more surgical. Instead of just arguing, I proposed we create a decision matrix. We listed criteria like deployment complexity, team expertise, latency requirements, and timeline. We scored each approach objectively. The data showed a hybrid approach was best: extract the highest-traffic component first while keeping the rest as a modular monolith. My colleague appreciated the structured approach, and we ended up with a better solution than either of us initially proposed."),
    
    # Message 15-16: Learning and growth
    (INTERVIEWER_ID, "That's a mature approach to conflict. How do you stay current with new technologies? The field moves fast."),
    (CANDIDATE_ID, "I have a few strategies. I dedicate Friday afternoons to learning. Sometimes it's reading papers, sometimes building small prototypes. I follow key people on social media and read their blogs. I also participate in our internal tech talks, both presenting and attending. For deeper learning, I contribute to open source. I maintain a small library for async HTTP caching that has about 500 stars on GitHub. Teaching is learning, so when I learn something new, I try to write about it or present it to the team. Conferences are valuable too. I try to attend one major conference per year, even if just virtually."),
    
    # Message 17-18: Specific scenario
    (INTERVIEWER_ID, "You mentioned async programming. Can you explain a tricky bug you encountered with async code and how you solved it?"),
    (CANDIDATE_ID, "Oh, I have a good one. We had a memory leak that only appeared under sustained load. After hours of profiling, I discovered we were creating thousands of tasks but never awaiting them. The issue was a fire-and-forget pattern where we'd call create_task but the tasks would accumulate if they completed faster than we checked them. The fix was to use asyncio.TaskGroup, which was new in Python 3.11. It ensures all tasks are properly awaited and handles cancellation correctly. The deeper lesson was that async code requires careful lifecycle management. Now I always use structured concurrency patterns and have a linting rule that flags bare create_task calls."),
    
    # Message 19-20: Closing
    (INTERVIEWER_ID, "Excellent debugging story. We're coming up on time. Do you have any questions for me about the team or the role?"),
    (CANDIDATE_ID, "Yes, I have a few. First, what does success look like in this role after six months? Second, how does the team handle technical debt? Is there dedicated time for refactoring, or is it more opportunistic? And finally, I'm curious about the team culture. How do you balance moving fast with maintaining quality?"),
]


# =============================================================================
# Simulation State
# =============================================================================

class SimulationState(str, Enum):
    """Simulation state enumeration"""
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class SentMessage:
    """Record of a sent message"""
    index: int
    speaker_id: str
    text: str
    event: dict
    sent_at: str
    audio_offset_ms: float


# =============================================================================
# Event Generation
# =============================================================================

def generate_transcript_event(
    speaker_id: str,
    text: str,
    event_type: str = "final",
    audio_offset_ms: float = 0.0,
) -> dict:
    """Generate a v2 transcript event matching C# bot output format."""
    word_count = len(text.split())
    duration_ms = word_count * 60 + random.uniform(50, 150)  # ~60ms per word
    
    return {
        "event_type": event_type,
        "text": text,
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "speaker_id": speaker_id,
        "audio_start_ms": round(audio_offset_ms, 1),
        "audio_end_ms": round(audio_offset_ms + duration_ms, 1),
        "confidence": round(random.uniform(0.88, 0.98), 3),
        "metadata": {
            "provider": "deepgram",
            "model": "nova-3",
        },
    }


def generate_session_event(event_type: str) -> dict:
    """Generate session start/stop event."""
    return {
        "event_type": event_type,
        "text": None,
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "speaker_id": None,
        "metadata": {
            "provider": "deepgram",
        },
    }


def calculate_delay(speaker_id: str, text: str) -> float:
    """
    Calculate realistic delay based on speaker and message length.
    
    Interviewer: 2-3 seconds (faster pace for questions)
    Candidate: 3-5 seconds (slower, thoughtful responses), scaled by length
    """
    if speaker_id == INTERVIEWER_ID:
        return random.uniform(INTERVIEWER_DELAY_MIN, INTERVIEWER_DELAY_MAX)
    
    # Candidate delay scales with message length
    word_count = len(text.split())
    base_delay = word_count / CANDIDATE_WORDS_PER_SECOND
    
    # Clamp to reasonable range with some randomness
    delay = max(CANDIDATE_DELAY_MIN, min(CANDIDATE_DELAY_MAX, base_delay))
    delay += random.uniform(-0.5, 0.5)  # Add jitter
    
    return max(CANDIDATE_DELAY_MIN, min(CANDIDATE_DELAY_MAX, delay))


# =============================================================================
# Simulation Engine
# =============================================================================

class SimulationEngine:
    """
    Stateful interview simulation engine for Streamlit integration.
    
    Thread-safe state management with async control methods for
    start/stop/restart functionality.
    
    Usage:
        engine = SimulationEngine()
        await engine.start()  # Start from beginning
        await engine.stop()   # Pause at current position
        await engine.start()  # Resume from paused position
        await engine.restart()  # Reset and start fresh
    """
    
    def __init__(
        self,
        sink_url: str = SINK_URL,
        candidate_name: str = CANDIDATE_NAME,
        meeting_url: str = MEETING_URL,
    ):
        self.sink_url = sink_url
        self.candidate_name = candidate_name
        self.meeting_url = meeting_url
        
        # State
        self._state = SimulationState.IDLE
        self._current_index = 0
        self._messages_sent: list[SentMessage] = []
        self._audio_offset_ms = 0.0
        self._session_id: Optional[str] = None
        self._error: Optional[str] = None
        
        # Threading
        self._lock = threading.Lock()
        self._stop_event = asyncio.Event()
        self._running_task: Optional[asyncio.Task] = None
        
        logger.info(f"SimulationEngine initialized: sink={sink_url}, candidate={candidate_name}")
    
    # -------------------------------------------------------------------------
    # Properties (thread-safe)
    # -------------------------------------------------------------------------
    
    @property
    def is_running(self) -> bool:
        """Whether simulation is actively running (not paused)"""
        with self._lock:
            return self._state == SimulationState.RUNNING
    
    @property
    def is_paused(self) -> bool:
        """Whether simulation is paused"""
        with self._lock:
            return self._state == SimulationState.PAUSED
    
    @property
    def is_completed(self) -> bool:
        """Whether simulation has completed"""
        with self._lock:
            return self._state == SimulationState.COMPLETED
    
    @property
    def current_index(self) -> int:
        """Current message index (0-based)"""
        with self._lock:
            return self._current_index
    
    @property
    def messages_sent(self) -> list[SentMessage]:
        """List of messages sent so far"""
        with self._lock:
            return list(self._messages_sent)
    
    @property
    def state(self) -> SimulationState:
        """Current simulation state"""
        with self._lock:
            return self._state
    
    @property
    def progress(self) -> tuple[int, int]:
        """Current progress as (current, total) tuple"""
        with self._lock:
            return (self._current_index, len(INTERVIEW_SCRIPT))
    
    @property
    def session_id(self) -> Optional[str]:
        """Current session ID from sink"""
        with self._lock:
            return self._session_id
    
    @property
    def error(self) -> Optional[str]:
        """Error message if in error state"""
        with self._lock:
            return self._error
    
    # -------------------------------------------------------------------------
    # Control Methods
    # -------------------------------------------------------------------------
    
    async def start(self) -> None:
        """
        Start simulation from beginning or resume if paused.
        
        If IDLE or COMPLETED: starts fresh from index 0
        If PAUSED: resumes from current position
        If RUNNING: no-op
        """
        with self._lock:
            if self._state == SimulationState.RUNNING:
                logger.info("Simulation already running")
                return
            
            if self._state in (SimulationState.IDLE, SimulationState.COMPLETED, SimulationState.ERROR):
                # Start fresh
                self._current_index = 0
                self._messages_sent = []
                self._audio_offset_ms = 0.0
                self._session_id = None
                self._error = None
            
            # Resume from paused or start fresh
            self._state = SimulationState.RUNNING
            self._stop_event.clear()
        
        logger.info(f"Starting simulation from index {self._current_index}")
        
        # Start the simulation loop
        self._running_task = asyncio.create_task(self._run_loop())
    
    async def stop(self) -> None:
        """
        Pause the simulation at current position.
        
        The simulation can be resumed with start().
        """
        with self._lock:
            if self._state != SimulationState.RUNNING:
                logger.info(f"Cannot stop: state is {self._state}")
                return
            
            self._state = SimulationState.PAUSED
            self._stop_event.set()
        
        logger.info(f"Simulation paused at index {self._current_index}")
        
        # Wait for running task to acknowledge stop
        if self._running_task:
            try:
                await asyncio.wait_for(self._running_task, timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("Simulation task did not stop cleanly")
            self._running_task = None
    
    async def restart(self) -> None:
        """
        Reset to beginning and start fresh.
        
        Stops current simulation (if running), resets state, and starts over.
        """
        # Stop if running
        if self.is_running:
            await self.stop()
        
        with self._lock:
            self._state = SimulationState.IDLE
            self._current_index = 0
            self._messages_sent = []
            self._audio_offset_ms = 0.0
            self._session_id = None
            self._error = None
        
        logger.info("Simulation reset to beginning")
        
        # Start fresh
        await self.start()
    
    async def run_step(self) -> Optional[dict]:
        """
        Execute one step of simulation.
        
        Returns:
            The message event dict that was sent, or None if:
            - Simulation is not running
            - Simulation is complete
            - An error occurred
        """
        with self._lock:
            if self._state != SimulationState.RUNNING:
                return None
            
            if self._current_index >= len(INTERVIEW_SCRIPT):
                self._state = SimulationState.COMPLETED
                return None
            
            index = self._current_index
            speaker_id, text = INTERVIEW_SCRIPT[index]
            audio_offset = self._audio_offset_ms
        
        # Generate and send event
        event = generate_transcript_event(
            speaker_id=speaker_id,
            text=text,
            audio_offset_ms=audio_offset,
        )
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(f"{self.sink_url}/transcript", json=event)
                if resp.status_code != 200:
                    logger.warning(f"Failed to send transcript: {resp.status_code} - {resp.text}")
        except Exception as e:
            logger.error(f"Error sending transcript: {e}")
            with self._lock:
                self._state = SimulationState.ERROR
                self._error = str(e)
            return None
        
        # Record sent message
        word_count = len(text.split())
        sent_msg = SentMessage(
            index=index,
            speaker_id=speaker_id,
            text=text,
            event=event,
            sent_at=event["timestamp_utc"],
            audio_offset_ms=audio_offset,
        )
        
        with self._lock:
            self._messages_sent.append(sent_msg)
            self._current_index += 1
            self._audio_offset_ms += word_count * 60 + 500  # Add 500ms pause between messages
        
        role = "Interviewer" if speaker_id == INTERVIEWER_ID else "Candidate"
        logger.info(f"[{index + 1}/{len(INTERVIEW_SCRIPT)}] {role}: {text[:80]}...")
        
        return event
    
    # -------------------------------------------------------------------------
    # Internal Methods
    # -------------------------------------------------------------------------
    
    async def _run_loop(self) -> None:
        """
        Main simulation loop with timing and pause support.
        """
        try:
            # Initialize session if starting fresh
            if self._current_index == 0:
                await self._initialize_session()
            
            while True:
                # Check for stop signal
                if self._stop_event.is_set():
                    logger.info("Stop signal received")
                    break
                
                # Check if complete
                with self._lock:
                    if self._current_index >= len(INTERVIEW_SCRIPT):
                        self._state = SimulationState.COMPLETED
                        break
                    
                    speaker_id, text = INTERVIEW_SCRIPT[self._current_index]
                
                # Execute step
                event = await self.run_step()
                if event is None:
                    break
                
                # Calculate and apply delay
                delay = calculate_delay(speaker_id, text)
                
                # Wait with interruptible sleep
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=delay
                    )
                    # Stop event was set
                    break
                except asyncio.TimeoutError:
                    # Normal timeout, continue
                    pass
            
            # Finalize session if completed
            with self._lock:
                if self._state == SimulationState.COMPLETED:
                    asyncio.create_task(self._finalize_session())
        
        except Exception as e:
            logger.error(f"Error in simulation loop: {e}", exc_info=True)
            with self._lock:
                self._state = SimulationState.ERROR
                self._error = str(e)
    
    async def _initialize_session(self) -> None:
        """Initialize session with transcript sink."""
        logger.info(f"Initializing session for: {self.candidate_name}")
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Check sink health
            try:
                resp = await client.get(f"{self.sink_url}/health")
                if resp.status_code != 200:
                    raise RuntimeError(f"Sink not healthy: {resp.status_code}")
                logger.info(f"Sink healthy: {resp.json()}")
            except httpx.ConnectError:
                raise RuntimeError(f"Cannot connect to sink at {self.sink_url}")
            
            # Start session
            resp = await client.post(
                f"{self.sink_url}/session/start",
                json={
                    "candidate_name": self.candidate_name,
                    "meeting_url": self.meeting_url,
                }
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to start session: {resp.text}")
            
            session_data = resp.json()
            with self._lock:
                self._session_id = session_data.get("session_id")
            
            logger.info(f"Session started: {self._session_id}")
            
            # Map speakers
            await client.post(
                f"{self.sink_url}/session/map-speaker",
                json={"speaker_id": INTERVIEWER_ID, "role": "interviewer"}
            )
            await client.post(
                f"{self.sink_url}/session/map-speaker",
                json={"speaker_id": CANDIDATE_ID, "role": "candidate"}
            )
            logger.info("Speakers mapped: speaker_0=interviewer, speaker_1=candidate")
            
            # Send session started event
            await client.post(
                f"{self.sink_url}/transcript",
                json=generate_session_event("session_started")
            )
            logger.info("Session started event sent")
    
    async def _finalize_session(self) -> None:
        """Finalize session with transcript sink."""
        logger.info("Finalizing session...")
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Send session stopped event
                await client.post(
                    f"{self.sink_url}/transcript",
                    json=generate_session_event("session_stopped")
                )
                
                await asyncio.sleep(0.5)
                
                # Get final stats
                stats_resp = await client.get(f"{self.sink_url}/stats")
                if stats_resp.status_code == 200:
                    stats = stats_resp.json()
                    logger.info(f"Final stats: {stats['stats']}")
                
                # End the session
                end_resp = await client.post(f"{self.sink_url}/session/end")
                if end_resp.status_code == 200:
                    summary = end_resp.json().get("summary", {})
                    logger.info(f"Session ended: {summary}")
        
        except Exception as e:
            logger.error(f"Error finalizing session: {e}")
    
    # -------------------------------------------------------------------------
    # Status Methods
    # -------------------------------------------------------------------------
    
    def get_status(self) -> dict:
        """
        Get current simulation status as a dictionary.
        
        Useful for Streamlit session state updates.
        """
        with self._lock:
            return {
                "state": self._state.value,
                "is_running": self._state == SimulationState.RUNNING,
                "is_paused": self._state == SimulationState.PAUSED,
                "is_completed": self._state == SimulationState.COMPLETED,
                "current_index": self._current_index,
                "total_messages": len(INTERVIEW_SCRIPT),
                "progress_pct": round(self._current_index / len(INTERVIEW_SCRIPT) * 100, 1),
                "messages_sent_count": len(self._messages_sent),
                "session_id": self._session_id,
                "error": self._error,
            }
    
    def get_last_message(self) -> Optional[SentMessage]:
        """Get the most recently sent message."""
        with self._lock:
            if self._messages_sent:
                return self._messages_sent[-1]
            return None
    
    def get_transcript_so_far(self) -> str:
        """Get formatted transcript of all sent messages."""
        with self._lock:
            lines = []
            for msg in self._messages_sent:
                role = "Interviewer" if msg.speaker_id == INTERVIEWER_ID else "Candidate"
                lines.append(f"[{role}] {msg.text}")
            return "\n\n".join(lines)


# =============================================================================
# Singleton for Streamlit
# =============================================================================

_engine_instance: Optional[SimulationEngine] = None
_engine_lock = threading.Lock()


def get_simulation_engine() -> SimulationEngine:
    """
    Get or create the singleton SimulationEngine instance.
    
    Use this in Streamlit to ensure only one engine exists across reruns.
    """
    global _engine_instance
    with _engine_lock:
        if _engine_instance is None:
            _engine_instance = SimulationEngine()
        return _engine_instance


def reset_simulation_engine() -> SimulationEngine:
    """
    Reset the singleton SimulationEngine instance.
    
    Creates a fresh engine instance.
    """
    global _engine_instance
    with _engine_lock:
        _engine_instance = SimulationEngine()
        return _engine_instance


# =============================================================================
# CLI Test
# =============================================================================

async def _test_simulation():
    """Test simulation with start, pause, resume, and restart."""
    engine = SimulationEngine()
    
    print(f"\n{'='*60}")
    print("Testing SimulationEngine")
    print(f"{'='*60}\n")
    
    # Test 1: Start and run 5 messages
    print("Test 1: Start simulation, run 5 messages...")
    await engine.start()
    
    while engine.current_index < 5 and engine.is_running:
        await asyncio.sleep(0.5)
    
    await engine.stop()
    print(f"  Status: {engine.get_status()}")
    print(f"  Messages sent: {engine.current_index}")
    
    # Test 2: Resume and run 3 more
    print("\nTest 2: Resume simulation, run 3 more messages...")
    await engine.start()
    
    target = engine.current_index + 3
    while engine.current_index < target and engine.is_running:
        await asyncio.sleep(0.5)
    
    await engine.stop()
    print(f"  Status: {engine.get_status()}")
    print(f"  Messages sent: {engine.current_index}")
    
    # Test 3: Restart
    print("\nTest 3: Restart simulation...")
    await engine.restart()
    
    while engine.current_index < 2 and engine.is_running:
        await asyncio.sleep(0.5)
    
    await engine.stop()
    print(f"  Status: {engine.get_status()}")
    print(f"  Messages sent: {engine.current_index}")
    
    print(f"\n{'='*60}")
    print("Tests complete!")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    print("Run with: uv run python simulation_engine.py")
    print("Or import SimulationEngine in your code.")
    print("\nRunning test simulation...")
    asyncio.run(_test_simulation())
