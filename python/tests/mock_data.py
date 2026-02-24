"""
Mock data generators for LegionMeet testing.

Generates realistic TranscriptEvent objects matching v2 format with diarization,
simulating real interview conversations between interviewer and candidate.

Last Grunted: 01/31/2026
"""

import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from interview_agent.models import (
    TranscriptEvent,
    EventMetadata,
    EventError,
    AnalysisItem,
    SpeakerMapping,
    InterviewSession,
    SessionAnalysis,
)


# =============================================================================
# Interview Q&A Database - Realistic Technical Interview Content
# =============================================================================

INTERVIEWER_QUESTIONS = [
    "Can you tell me about your experience with Python and what projects you've worked on?",
    "How do you approach debugging a complex distributed system issue?",
    "Describe a challenging technical problem you solved recently.",
    "What's your experience with cloud platforms like AWS or Azure?",
    "How do you ensure code quality in your projects?",
    "Tell me about a time you had to learn a new technology quickly.",
    "How do you handle disagreements with team members on technical decisions?",
    "What's your experience with CI/CD pipelines and DevOps practices?",
    "Can you walk me through how you'd design a real-time data processing system?",
    "What testing strategies do you typically use?",
    "How do you stay current with new technologies and best practices?",
    "Describe your experience with microservices architecture.",
    "What's your approach to writing maintainable and scalable code?",
    "How do you prioritize tasks when you have multiple deadlines?",
    "Tell me about your experience leading or mentoring other developers.",
]

CANDIDATE_RESPONSES = [
    "I have 5 years of Python experience, primarily in backend development. Most recently, I built a real-time analytics pipeline using FastAPI and Apache Kafka that processes over 2 million events per day. I also contributed to an open-source async library for PostgreSQL.",
    "When debugging distributed systems, I start by establishing a timeline of events across services using distributed tracing. I check logs from each service involved, looking for correlation IDs. I've found that most issues stem from network partitions or clock drift, so I verify those early.",
    "Last quarter, I tackled a memory leak in our Python service that only appeared under high load. After extensive profiling with py-spy and memory_profiler, I discovered we were holding references to closed database connections. The fix involved implementing proper context managers and connection pooling.",
    "I've worked extensively with AWS for the past 3 years, using services like Lambda, ECS, DynamoDB, and SQS. I also have production experience with Azure Functions and Cosmos DB from my previous role. I'm comfortable with infrastructure as code using Terraform.",
    "I'm a strong advocate for code review, automated testing, and static analysis. I typically aim for 80% test coverage with meaningful tests, not just line coverage. I use tools like mypy for type checking and ruff for linting. I also believe in writing clear documentation.",
    "At my previous company, we migrated from a monolith to Kubernetes in 3 months. I had no prior K8s experience but learned through official documentation, hands-on labs, and pair programming with our DevOps engineer. Within a month, I was deploying production workloads.",
    "I focus on understanding the other person's perspective first. In a recent debate about database choice, I created a comparison matrix with actual benchmarks rather than relying on opinions. Data-driven discussions usually lead to consensus.",
    "I've set up CI/CD pipelines using GitHub Actions, GitLab CI, and Jenkins. My preferred setup includes automated testing, security scanning with Snyk, and progressive rollouts with feature flags. I believe in deploying small changes frequently.",
    "For real-time data processing, I'd consider the throughput requirements first. For high volume, I'd use Kafka for ingestion with a stream processor like Flink or Spark Streaming. For lower volume, even a well-tuned PostgreSQL with LISTEN/NOTIFY can work. The key is understanding latency requirements.",
    "I follow the testing pyramid: lots of unit tests, fewer integration tests, and minimal E2E tests. For async code, I use pytest-asyncio with controlled event loops. I also write property-based tests with Hypothesis for edge case discovery.",
    "I dedicate time each week to reading technical blogs, following key people on social media, and experimenting with new tools. I also participate in our company's tech talks and try to attend one conference per year.",
    "I've designed and maintained microservices for the past 4 years. Key lessons: define clear API contracts upfront, implement proper observability from day one, and use event-driven communication to reduce coupling. I also learned the hard way that distributed transactions should be avoided.",
    "I follow SOLID principles and favor composition over inheritance. I write code assuming the next person reading it won't have context. Clear naming, small functions, and comprehensive type hints make a huge difference. I also refactor continuously rather than letting tech debt accumulate.",
    "I use a combination of urgency and impact assessment. I communicate early if I see conflicts, and I'm not afraid to push back on unrealistic timelines with data. I also try to identify tasks that can be parallelized or delegated.",
    "I've mentored 3 junior developers and led a team of 5 for a year-long project. I believe in pairing sessions for knowledge transfer and creating a safe environment for questions. Regular 1:1s help me understand each person's growth goals.",
]

FOLLOW_UP_QUESTIONS = [
    "Can you elaborate on that?",
    "Interesting. How did that work in practice?",
    "What challenges did you face with that approach?",
    "That's great. How long did that take to implement?",
    "What would you do differently if you could start over?",
]


# =============================================================================
# Timestamp Generation
# =============================================================================

def _iso_timestamp(offset_seconds: float = 0) -> str:
    """Generate ISO 8601 UTC timestamp with optional offset."""
    ts = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond:06d}"[:3] + "Z"


def _generate_audio_timing(duration_ms: float, offset_ms: float = 0) -> tuple[float, float]:
    """Generate realistic audio start/end times."""
    start = offset_ms + random.uniform(0, 100)
    end = start + duration_ms
    return round(start, 1), round(end, 1)


# =============================================================================
# Event Generators
# =============================================================================

def generate_session_start_event(
    timestamp_offset: float = 0,
    provider: str = "deepgram",
) -> TranscriptEvent:
    """
    Generate a session_started event.
    
    Args:
        timestamp_offset: Seconds to offset from current time.
        provider: STT provider name for metadata.
        
    Returns:
        TranscriptEvent with event_type="session_started"
    """
    return TranscriptEvent(
        event_type="session_started",
        text=None,
        timestamp_utc=_iso_timestamp(timestamp_offset),
        speaker_id=None,
        audio_start_ms=None,
        audio_end_ms=None,
        confidence=None,
        metadata=EventMetadata(
            provider=provider,
            raw_response={"session_id": f"sess_{random.randint(10000, 99999)}"},
        ),
    )


def generate_session_stop_event(
    timestamp_offset: float = 0,
    provider: str = "deepgram",
) -> TranscriptEvent:
    """
    Generate a session_stopped event.
    
    Args:
        timestamp_offset: Seconds to offset from current time.
        provider: STT provider name for metadata.
        
    Returns:
        TranscriptEvent with event_type="session_stopped"
    """
    return TranscriptEvent(
        event_type="session_stopped",
        text=None,
        timestamp_utc=_iso_timestamp(timestamp_offset),
        speaker_id=None,
        audio_start_ms=None,
        audio_end_ms=None,
        confidence=None,
        metadata=EventMetadata(
            provider=provider,
        ),
    )


def generate_transcript_event(
    speaker_id: str,
    text: str,
    event_type: str = "final",
    timestamp_offset: float = 0,
    audio_offset_ms: float = 0,
    confidence: Optional[float] = None,
    provider: str = "deepgram",
    model: str = "nova-3",
) -> TranscriptEvent:
    """
    Generate a transcript event matching C# bot v2 format.
    
    Args:
        speaker_id: Speaker identifier (e.g., "speaker_0").
        text: Transcript text content.
        event_type: Event type ("partial" or "final").
        timestamp_offset: Seconds to offset from current time.
        audio_offset_ms: Audio position offset in milliseconds.
        confidence: Recognition confidence (0-1). Auto-generated if None.
        provider: STT provider name.
        model: STT model name.
        
    Returns:
        TranscriptEvent matching v2 format.
    """
    # Estimate duration based on text length (avg 150 words/min = 400ms/word)
    word_count = len(text.split())
    duration_ms = word_count * 400 + random.uniform(-50, 50)
    
    audio_start, audio_end = _generate_audio_timing(duration_ms, audio_offset_ms)
    
    if confidence is None:
        # Realistic confidence distribution (mostly high, occasionally lower)
        confidence = min(1.0, max(0.5, random.gauss(0.92, 0.05)))
    
    return TranscriptEvent(
        event_type=event_type,
        text=text,
        timestamp_utc=_iso_timestamp(timestamp_offset),
        speaker_id=speaker_id,
        audio_start_ms=audio_start,
        audio_end_ms=audio_end,
        confidence=round(confidence, 3),
        metadata=EventMetadata(
            provider=provider,
            raw_response={"model": model},
        ),
    )


def generate_interview_conversation(
    candidate_name: str = "Alex Johnson",
    num_exchanges: int = 5,
    interviewer_speaker_id: str = "speaker_0",
    candidate_speaker_id: str = "speaker_1",
    include_session_events: bool = True,
    include_partials: bool = False,
) -> list[TranscriptEvent]:
    """
    Generate a complete interview conversation simulation.
    
    Produces a realistic sequence of transcript events including
    interviewer questions and candidate responses.
    
    Args:
        candidate_name: Name of the candidate (for context).
        num_exchanges: Number of Q&A exchanges to generate.
        interviewer_speaker_id: Speaker ID for interviewer.
        candidate_speaker_id: Speaker ID for candidate.
        include_session_events: Include session start/stop events.
        include_partials: Include partial transcript events.
        
    Returns:
        List of TranscriptEvent objects in chronological order.
    """
    events: list[TranscriptEvent] = []
    audio_offset = 0.0
    time_offset = 0.0
    
    # Select random questions and responses
    questions = random.sample(INTERVIEWER_QUESTIONS, min(num_exchanges, len(INTERVIEWER_QUESTIONS)))
    responses = random.sample(CANDIDATE_RESPONSES, min(num_exchanges, len(CANDIDATE_RESPONSES)))
    
    # Session start
    if include_session_events:
        events.append(generate_session_start_event(timestamp_offset=time_offset))
        time_offset += 1.0
    
    # Opening from interviewer
    opening = f"Thanks for joining today. I'm the hiring manager for this position. Let's get started."
    events.append(generate_transcript_event(
        speaker_id=interviewer_speaker_id,
        text=opening,
        timestamp_offset=time_offset,
        audio_offset_ms=audio_offset,
    ))
    
    # Update offsets
    audio_offset += len(opening.split()) * 400 + 500
    time_offset += 3.0
    
    # Q&A exchanges
    for i in range(num_exchanges):
        question = questions[i]
        response = responses[i]
        
        # Optional: Generate partial for question
        if include_partials and random.random() > 0.5:
            partial_text = " ".join(question.split()[:3]) + "..."
            events.append(generate_transcript_event(
                speaker_id=interviewer_speaker_id,
                text=partial_text,
                event_type="partial",
                timestamp_offset=time_offset,
                audio_offset_ms=audio_offset,
            ))
            time_offset += 0.5
        
        # Interviewer question (final)
        events.append(generate_transcript_event(
            speaker_id=interviewer_speaker_id,
            text=question,
            timestamp_offset=time_offset,
            audio_offset_ms=audio_offset,
        ))
        
        audio_offset += len(question.split()) * 400 + 1000  # + pause
        time_offset += len(question.split()) * 0.4 + 2.0
        
        # Optional: Generate partial for response
        if include_partials and random.random() > 0.5:
            partial_text = " ".join(response.split()[:5]) + "..."
            events.append(generate_transcript_event(
                speaker_id=candidate_speaker_id,
                text=partial_text,
                event_type="partial",
                timestamp_offset=time_offset,
                audio_offset_ms=audio_offset,
            ))
            time_offset += 0.5
        
        # Candidate response (final)
        events.append(generate_transcript_event(
            speaker_id=candidate_speaker_id,
            text=response,
            timestamp_offset=time_offset,
            audio_offset_ms=audio_offset,
        ))
        
        audio_offset += len(response.split()) * 400 + 1500  # + longer pause
        time_offset += len(response.split()) * 0.4 + 3.0
        
        # Occasional follow-up
        if i < num_exchanges - 1 and random.random() > 0.7:
            follow_up = random.choice(FOLLOW_UP_QUESTIONS)
            events.append(generate_transcript_event(
                speaker_id=interviewer_speaker_id,
                text=follow_up,
                timestamp_offset=time_offset,
                audio_offset_ms=audio_offset,
            ))
            audio_offset += len(follow_up.split()) * 400 + 500
            time_offset += 2.0
    
    # Closing from interviewer
    closing = "That wraps up our questions. Do you have any questions for us?"
    events.append(generate_transcript_event(
        speaker_id=interviewer_speaker_id,
        text=closing,
        timestamp_offset=time_offset,
        audio_offset_ms=audio_offset,
    ))
    
    audio_offset += len(closing.split()) * 400 + 500
    time_offset += 3.0
    
    # Session stop
    if include_session_events:
        events.append(generate_session_stop_event(timestamp_offset=time_offset))
    
    return events


# =============================================================================
# Analysis Mock Generators
# =============================================================================

def generate_analysis_item(
    response_id: Optional[str] = None,
    response_text: Optional[str] = None,
    question_text: Optional[str] = None,
    speaker_id: str = "speaker_1",
) -> AnalysisItem:
    """
    Generate a mock AnalysisItem with realistic scores.
    
    Args:
        response_id: Unique ID for the response. Auto-generated if None.
        response_text: Candidate response text. Random if None.
        question_text: Interviewer question text. Random if None.
        speaker_id: Speaker ID of the candidate.
        
    Returns:
        AnalysisItem with realistic analysis data.
    """
    if response_id is None:
        response_id = f"resp_{random.randint(1000, 9999)}"
    
    if response_text is None:
        response_text = random.choice(CANDIDATE_RESPONSES)
    
    if question_text is None:
        question_text = random.choice(INTERVIEWER_QUESTIONS)
    
    # Generate realistic scores (mostly good, some variation)
    relevance = min(1.0, max(0.0, random.gauss(0.82, 0.10)))
    clarity = min(1.0, max(0.0, random.gauss(0.85, 0.08)))
    
    # Extract key points (simple simulation)
    sentences = response_text.split(". ")
    key_points = [s.strip()[:60] + "..." for s in sentences[:3] if len(s) > 20]
    
    # Generate follow-up suggestions
    follow_ups = [
        "Ask for specific examples or metrics",
        "Probe deeper on technical implementation details",
        "Clarify the timeline and scope of the project",
    ]
    
    return AnalysisItem(
        response_id=response_id,
        question_text=question_text,
        response_text=response_text,
        speaker_id=speaker_id,
        relevance_score=round(relevance, 2),
        clarity_score=round(clarity, 2),
        key_points=key_points,
        follow_up_suggestions=random.sample(follow_ups, k=random.randint(1, 2)),
    )


def generate_session_analysis(
    session_id: Optional[str] = None,
    candidate_name: str = "Alex Johnson",
    num_items: int = 5,
) -> SessionAnalysis:
    """
    Generate a complete mock SessionAnalysis.
    
    Args:
        session_id: Unique session ID. Auto-generated if None.
        candidate_name: Name of the candidate.
        num_items: Number of analysis items to generate.
        
    Returns:
        SessionAnalysis with computed overall scores.
    """
    if session_id is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        session_id = f"int_{ts}_{random.randint(1000, 9999):04x}"
    
    started_at = _iso_timestamp(-3600)  # 1 hour ago
    ended_at = _iso_timestamp(-60)  # 1 minute ago
    
    items = [generate_analysis_item() for _ in range(num_items)]
    
    analysis = SessionAnalysis(
        session_id=session_id,
        candidate_name=candidate_name,
        started_at=started_at,
        ended_at=ended_at,
        analysis_items=items,
    )
    
    analysis.compute_overall_scores()
    return analysis


# =============================================================================
# Convenience Functions
# =============================================================================

def generate_v2_event_dict(
    speaker_id: str = "speaker_0",
    text: str = "Test transcript text",
    event_type: str = "final",
) -> dict:
    """
    Generate a dictionary matching the exact v2 JSON format from C# bot.
    
    Useful for testing JSON parsing and API endpoints.
    
    Returns:
        Dictionary matching v2 transcript event JSON schema.
    """
    word_count = len(text.split())
    audio_start = random.uniform(1000, 5000)
    audio_end = audio_start + word_count * 400
    
    return {
        "event_type": event_type,
        "text": text,
        "timestamp_utc": _iso_timestamp(),
        "speaker_id": speaker_id,
        "audio_start_ms": round(audio_start, 1),
        "audio_end_ms": round(audio_end, 1),
        "confidence": round(random.uniform(0.88, 0.98), 2),
        "metadata": {"provider": "deepgram", "model": "nova-3"},
    }


def generate_v1_event_dict(
    kind: str = "recognized",
    text: str = "Test transcript text",
) -> dict:
    """
    Generate a dictionary matching v1 JSON format (legacy C# bot format).
    
    Returns:
        Dictionary matching v1 transcript event JSON schema.
    """
    return {
        "Kind": kind,
        "Text": text,
        "TsUtc": _iso_timestamp(),
    }
