#!/usr/bin/env python3
"""
Synthetic Interview Conversation Simulator

Streams 20 realistic interview messages to the FastAPI transcript sink,
simulating exactly how data would arrive from a live Teams meeting.

Usage:
    # Start the transcript sink first:
    uv run python transcript_sink.py

    # In another terminal, run the simulator:
    uv run python simulate_interview.py
"""

import asyncio
import httpx
import random
from datetime import datetime, timezone
from typing import Optional
import logging
import sys

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


# =============================================================================
# Simulation Runner
# =============================================================================

async def run_simulation():
    """Run the interview simulation, streaming events to the transcript sink."""
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        
        # Check if sink is running
        logger.info("Checking transcript sink health...")
        try:
            resp = await client.get(f"{SINK_URL}/health")
            if resp.status_code != 200:
                logger.error(f"Sink not healthy: {resp.status_code}")
                return
            logger.info(f"Sink healthy: {resp.json()}")
        except httpx.ConnectError:
            logger.error(f"Cannot connect to sink at {SINK_URL}. Is it running?")
            logger.error("Start it with: uv run python transcript_sink.py")
            return
        
        # Start interview session
        logger.info(f"\n{'='*60}")
        logger.info(f"Starting interview session for: {CANDIDATE_NAME}")
        logger.info(f"{'='*60}\n")
        
        resp = await client.post(
            f"{SINK_URL}/session/start",
            json={
                "candidate_name": CANDIDATE_NAME,
                "meeting_url": MEETING_URL,
            }
        )
        if resp.status_code != 200:
            logger.error(f"Failed to start session: {resp.text}")
            return
        session_data = resp.json()
        session_id = session_data.get("session_id")
        logger.info(f"Session started: {session_id}")
        
        # Map speakers
        await client.post(
            f"{SINK_URL}/session/map-speaker",
            json={"speaker_id": INTERVIEWER_ID, "role": "interviewer"}
        )
        await client.post(
            f"{SINK_URL}/session/map-speaker",
            json={"speaker_id": CANDIDATE_ID, "role": "candidate"}
        )
        logger.info("Speakers mapped: speaker_0=interviewer, speaker_1=candidate")
        
        # Send session started event
        await client.post(
            f"{SINK_URL}/transcript",
            json=generate_session_event("session_started")
        )
        logger.info("Session started event sent")
        
        await asyncio.sleep(1)
        
        # Stream the interview
        audio_offset = 0.0
        
        for i, (speaker_id, text) in enumerate(INTERVIEW_SCRIPT, 1):
            role = "Interviewer" if speaker_id == INTERVIEWER_ID else "Candidate"
            
            # Calculate realistic delay based on text length
            word_count = len(text.split())
            speaking_time = word_count * 0.06  # ~60ms per word for speech
            
            # Send the transcript event
            event = generate_transcript_event(
                speaker_id=speaker_id,
                text=text,
                audio_offset_ms=audio_offset,
            )
            
            logger.info(f"\n[{i}/20] {role}: {text[:80]}{'...' if len(text) > 80 else ''}")
            
            resp = await client.post(f"{SINK_URL}/transcript", json=event)
            if resp.status_code != 200:
                logger.warning(f"Failed to send event: {resp.text}")
            
            # Update audio offset
            audio_offset += word_count * 60 + 500  # Add 500ms pause between messages
            
            # Wait for realistic streaming delay
            # Shorter for interviewer (questions are quicker), longer for candidate (thoughtful responses)
            if speaker_id == CANDIDATE_ID:
                delay = min(speaking_time * 0.5, 3.0)  # Cap at 3 seconds for candidate
            else:
                delay = min(speaking_time * 0.3, 1.5)  # Cap at 1.5 seconds for interviewer
            
            await asyncio.sleep(delay)
        
        # Send session stopped event
        await client.post(
            f"{SINK_URL}/transcript",
            json=generate_session_event("session_stopped")
        )
        
        await asyncio.sleep(1)
        
        # Get final stats
        stats_resp = await client.get(f"{SINK_URL}/stats")
        stats = stats_resp.json()
        
        logger.info(f"\n{'='*60}")
        logger.info("Interview simulation complete!")
        logger.info(f"{'='*60}")
        logger.info(f"Total events received: {stats['stats']['events_received']}")
        logger.info(f"Final transcripts: {stats['stats']['final_transcripts']}")
        logger.info(f"Agent analyses: {stats['stats']['agent_analyses']}")
        logger.info(f"V2 events: {stats['stats']['v2_events']}")
        
        # End the session
        end_resp = await client.post(f"{SINK_URL}/session/end")
        if end_resp.status_code == 200:
            summary = end_resp.json().get("summary", {})
            logger.info(f"\nSession ended: {summary.get('session_id')}")
            logger.info(f"Analyses generated: {summary.get('analyses_generated', 0)}")
        
        # Show where to view analysis
        logger.info(f"\n{'='*60}")
        logger.info("View analysis at:")
        logger.info(f"  curl {SINK_URL}/session/status")
        logger.info(f"  Check output/ directory for analysis JSON")
        logger.info(f"{'='*60}")


def main():
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("Teams Interview Simulator")
    logger.info("=" * 60)
    logger.info(f"Target: {SINK_URL}")
    logger.info(f"Candidate: {CANDIDATE_NAME}")
    logger.info(f"Messages: {len(INTERVIEW_SCRIPT)}")
    logger.info("")
    
    try:
        asyncio.run(run_simulation())
    except KeyboardInterrupt:
        logger.info("\nSimulation interrupted")
        sys.exit(0)


if __name__ == "__main__":
    main()
