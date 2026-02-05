#!/usr/bin/env python3
"""
Synthetic Interview Conversation Simulator.

Streams realistic interview messages to the FastAPI transcript sink,
simulating exactly how data would arrive from a live Teams meeting.

Usage:
    # Start the transcript sink first:
    uv run python transcript_sink.py

    # In another terminal, run the simulator:
    uv run python simulate_interview.py

    # With custom options:
    uv run python simulate_interview.py --sink-url http://localhost:8765 --candidate "Jane Doe"
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Final

import httpx

# =============================================================================
# Logging Configuration
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger: logging.Logger = logging.getLogger(__name__)


# =============================================================================
# Exit Codes
# =============================================================================

EXIT_SUCCESS: Final[int] = 0
EXIT_CONNECTION_ERROR: Final[int] = 1
EXIT_SINK_UNHEALTHY: Final[int] = 2
EXIT_SESSION_ERROR: Final[int] = 3
EXIT_INTERRUPTED: Final[int] = 130  # Standard SIGINT exit code


# =============================================================================
# Configuration
# =============================================================================

DEFAULT_SINK_URL: Final[str] = "http://127.0.0.1:8765"
DEFAULT_CANDIDATE_NAME: Final[str] = "Sarah Chen"
DEFAULT_MEETING_URL: Final[str] = "https://teams.microsoft.com/l/meetup-join/simulated-interview-session"

# Speaker IDs (matching diarization format)
INTERVIEWER_ID: Final[str] = "speaker_0"
CANDIDATE_ID: Final[str] = "speaker_1"

# Timing constants
MS_PER_WORD: Final[float] = 60.0
PAUSE_BETWEEN_MESSAGES_MS: Final[float] = 500.0
MIN_DURATION_JITTER_MS: Final[float] = 50.0
MAX_DURATION_JITTER_MS: Final[float] = 150.0
MIN_CONFIDENCE: Final[float] = 0.88
MAX_CONFIDENCE: Final[float] = 0.98
CANDIDATE_DELAY_SCALE: Final[float] = 0.5
CANDIDATE_DELAY_MAX: Final[float] = 3.0
INTERVIEWER_DELAY_SCALE: Final[float] = 0.3
INTERVIEWER_DELAY_MAX: Final[float] = 1.5


# =============================================================================
# Synthetic Interview Conversation (20 messages)
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
# Event Generation
# =============================================================================

def _generate_duration_jitter() -> float:
    """Generate random duration jitter for realistic speech timing."""
    import random
    return random.uniform(MIN_DURATION_JITTER_MS, MAX_DURATION_JITTER_MS)


def _generate_confidence() -> float:
    """Generate random confidence score within realistic range."""
    import random
    return round(random.uniform(MIN_CONFIDENCE, MAX_CONFIDENCE), 3)


def generate_transcript_event(
    speaker_id: str,
    text: str,
    event_type: str = "final",
    audio_offset_ms: float = 0.0,
) -> dict[str, object]:
    """
    Generate a v2 transcript event matching C# bot output format.

    Args:
        speaker_id: The speaker identifier (e.g., "speaker_0").
        text: The transcript text content.
        event_type: Event type, typically "final" for complete transcripts.
        audio_offset_ms: Audio offset from session start in milliseconds.

    Returns:
        Dictionary containing the transcript event in v2 format.
    """
    word_count = len(text.split())
    duration_ms = word_count * MS_PER_WORD + _generate_duration_jitter()

    return {
        "event_type": event_type,
        "text": text,
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "speaker_id": speaker_id,
        "audio_start_ms": round(audio_offset_ms, 1),
        "audio_end_ms": round(audio_offset_ms + duration_ms, 1),
        "confidence": _generate_confidence(),
        "metadata": {
            "provider": "deepgram",
            "model": "nova-3",
        },
    }


def generate_session_event(event_type: str) -> dict[str, object]:
    """
    Generate session start/stop event.

    Args:
        event_type: The session event type (e.g., "session_started", "session_stopped").

    Returns:
        Dictionary containing the session event.
    """
    return {
        "event_type": event_type,
        "text": None,
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "speaker_id": None,
        "metadata": {
            "provider": "deepgram",
        },
    }


# =============================================================================
# Simulation Runner
# =============================================================================

async def run_simulation(
    sink_url: str,
    candidate_name: str,
    meeting_url: str,
) -> int:
    """
    Run the interview simulation, streaming events to the transcript sink.

    Args:
        sink_url: URL of the transcript sink service.
        candidate_name: Name of the interview candidate.
        meeting_url: URL of the Teams meeting.

    Returns:
        Exit code indicating success or failure.
    """
    total_messages = len(INTERVIEW_SCRIPT)

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Check if sink is running
        logger.info("Checking transcript sink health...")
        try:
            resp = await client.get(f"{sink_url}/health")
            if resp.status_code != 200:
                logger.error("Sink not healthy: %d", resp.status_code)
                return EXIT_SINK_UNHEALTHY
            logger.info("Sink healthy: %s", resp.json())
        except httpx.ConnectError:
            logger.error("Cannot connect to sink at %s. Is it running?", sink_url)
            logger.error("Start it with: uv run python transcript_sink.py")
            return EXIT_CONNECTION_ERROR

        # Start interview session
        logger.info("\n%s", "=" * 60)
        logger.info("Starting interview session for: %s", candidate_name)
        logger.info("%s\n", "=" * 60)

        try:
            resp = await client.post(
                f"{sink_url}/session/start",
                json={
                    "candidate_name": candidate_name,
                    "meeting_url": meeting_url,
                },
            )
        except httpx.RequestError as exc:
            logger.error("Failed to start session: %s", exc)
            return EXIT_SESSION_ERROR

        if resp.status_code != 200:
            logger.error("Failed to start session: %s", resp.text)
            return EXIT_SESSION_ERROR

        session_data: dict[str, object] = resp.json()
        session_id = session_data.get("session_id")
        logger.info("Session started: %s", session_id)

        # Map speakers
        await client.post(
            f"{sink_url}/session/map-speaker",
            json={"speaker_id": INTERVIEWER_ID, "role": "interviewer"},
        )
        await client.post(
            f"{sink_url}/session/map-speaker",
            json={"speaker_id": CANDIDATE_ID, "role": "candidate"},
        )
        logger.info("Speakers mapped: speaker_0=interviewer, speaker_1=candidate")

        # Send session started event
        await client.post(
            f"{sink_url}/transcript",
            json=generate_session_event("session_started"),
        )
        logger.info("Session started event sent")

        await asyncio.sleep(1)

        # Stream the interview
        audio_offset: float = 0.0

        for i, (speaker_id, text) in enumerate(INTERVIEW_SCRIPT, 1):
            role = "Interviewer" if speaker_id == INTERVIEWER_ID else "Candidate"

            # Calculate realistic delay based on text length
            word_count = len(text.split())
            speaking_time = word_count * (MS_PER_WORD / 1000)  # Convert to seconds

            # Send the transcript event
            event = generate_transcript_event(
                speaker_id=speaker_id,
                text=text,
                audio_offset_ms=audio_offset,
            )

            truncated_text = f"{text[:80]}..." if len(text) > 80 else text
            logger.info("\n[%d/%d] %s: %s", i, total_messages, role, truncated_text)

            resp = await client.post(f"{sink_url}/transcript", json=event)
            if resp.status_code != 200:
                logger.warning("Failed to send event: %s", resp.text)

            # Update audio offset
            audio_offset += word_count * MS_PER_WORD + PAUSE_BETWEEN_MESSAGES_MS

            # Wait for realistic streaming delay
            # Shorter for interviewer (questions are quicker), longer for candidate
            if speaker_id == CANDIDATE_ID:
                delay = min(speaking_time * CANDIDATE_DELAY_SCALE, CANDIDATE_DELAY_MAX)
            else:
                delay = min(speaking_time * INTERVIEWER_DELAY_SCALE, INTERVIEWER_DELAY_MAX)

            await asyncio.sleep(delay)

        # Send session stopped event
        await client.post(
            f"{sink_url}/transcript",
            json=generate_session_event("session_stopped"),
        )

        await asyncio.sleep(1)

        # Get final stats
        stats_resp = await client.get(f"{sink_url}/stats")
        stats: dict[str, object] = stats_resp.json()

        logger.info("\n%s", "=" * 60)
        logger.info("Interview simulation complete!")
        logger.info("%s", "=" * 60)

        stats_data = stats.get("stats", {})
        if isinstance(stats_data, dict):
            logger.info("Total events received: %s", stats_data.get("events_received"))
            logger.info("Final transcripts: %s", stats_data.get("final_transcripts"))
            logger.info("Agent analyses: %s", stats_data.get("agent_analyses"))
            logger.info("V2 events: %s", stats_data.get("v2_events"))

        # End the session
        end_resp = await client.post(f"{sink_url}/session/end")
        if end_resp.status_code == 200:
            summary: dict[str, object] = end_resp.json().get("summary", {})
            logger.info("\nSession ended: %s", summary.get("session_id"))
            logger.info("Analyses generated: %s", summary.get("analyses_generated", 0))

        # Show where to view analysis
        logger.info("\n%s", "=" * 60)
        logger.info("View analysis at:")
        logger.info("  curl %s/session/status", sink_url)
        logger.info("  Check output/ directory for analysis JSON")
        logger.info("%s", "=" * 60)

    return EXIT_SUCCESS


def main(
    sink_url: str | None = None,
    candidate_name: str | None = None,
    meeting_url: str | None = None,
) -> int:
    """
    Main entry point for the interview simulator.

    Args:
        sink_url: URL of the transcript sink (defaults to env var or localhost:8765).
        candidate_name: Name of the candidate (defaults to "Sarah Chen").
        meeting_url: Teams meeting URL (defaults to simulated URL).

    Returns:
        Exit code indicating success or failure.
    """
    # Resolve configuration with environment variable fallbacks
    resolved_sink_url = sink_url or os.environ.get("SINK_URL", DEFAULT_SINK_URL)
    resolved_candidate = candidate_name or os.environ.get("CANDIDATE_NAME", DEFAULT_CANDIDATE_NAME)
    resolved_meeting_url = meeting_url or os.environ.get("MEETING_URL", DEFAULT_MEETING_URL)

    logger.info("=" * 60)
    logger.info("Teams Interview Simulator")
    logger.info("=" * 60)
    logger.info("Target: %s", resolved_sink_url)
    logger.info("Candidate: %s", resolved_candidate)
    logger.info("Messages: %d", len(INTERVIEW_SCRIPT))
    logger.info("")

    try:
        exit_code = asyncio.run(
            run_simulation(
                sink_url=resolved_sink_url,
                candidate_name=resolved_candidate,
                meeting_url=resolved_meeting_url,
            )
        )
        return exit_code
    except KeyboardInterrupt:
        logger.info("\nSimulation interrupted")
        return EXIT_INTERRUPTED


def cli() -> None:
    """
    Command-line interface entry point with argument parsing.

    Provides CLI options for configuring the simulation.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Simulate an interview conversation by streaming transcripts to the sink.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Run with defaults
    uv run python simulate_interview.py

    # Custom sink URL
    uv run python simulate_interview.py --sink-url http://localhost:9000

    # Custom candidate name
    uv run python simulate_interview.py --candidate "Jane Doe"

Environment Variables:
    SINK_URL         Transcript sink URL (default: http://127.0.0.1:8765)
    CANDIDATE_NAME   Candidate name (default: Sarah Chen)
    MEETING_URL      Teams meeting URL
        """,
    )

    parser.add_argument(
        "--sink-url",
        type=str,
        default=None,
        help=f"Transcript sink URL (default: {DEFAULT_SINK_URL})",
    )
    parser.add_argument(
        "--candidate",
        type=str,
        default=None,
        dest="candidate_name",
        help=f"Candidate name (default: {DEFAULT_CANDIDATE_NAME})",
    )
    parser.add_argument(
        "--meeting-url",
        type=str,
        default=None,
        help="Teams meeting URL",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    exit_code = main(
        sink_url=args.sink_url,
        candidate_name=args.candidate_name,
        meeting_url=args.meeting_url,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    cli()
