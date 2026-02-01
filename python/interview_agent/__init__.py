"""
Interview Analysis Agent Package.

Provides real-time interview analysis using the OpenAI Agents SDK.

Components:
    - InterviewAnalyzer: Main agent for analyzing candidate responses
    - InterviewSessionManager: Manages session state and transcript history
    - AnalysisOutputWriter: Persists analysis results to JSON files
    - Models: Pydantic models for transcripts, sessions, and analysis items

Example:
    >>> from interview_agent import InterviewAnalyzer
    >>> 
    >>> analyzer = InterviewAnalyzer()
    >>> result = await analyzer.analyze_async(
    ...     response_text="I have 5 years of experience with Python...",
    ...     context={
    ...         "candidate_name": "John Smith",
    ...         "conversation_history": [
    ...             {"role": "interviewer", "text": "Tell me about your Python experience."}
    ...         ]
    ...     }
    ... )
    >>> print(f"Relevance: {result.relevance_score}, Clarity: {result.clarity_score}")

Last Grunted: 01/31/2026
"""

from .models import (
    TranscriptEvent,
    EventMetadata,
    EventError,
    SpeakerMapping,
    InterviewSession,
    AnalysisItem,
    SessionAnalysis,
)

from .session import InterviewSessionManager

from .output import AnalysisOutputWriter

from .agent import (
    InterviewAnalyzer,
    InterviewAnalysisOutput,
    create_interview_analyzer,
)


__all__ = [
    # Models
    "TranscriptEvent",
    "EventMetadata",
    "EventError",
    "SpeakerMapping",
    "InterviewSession",
    "AnalysisItem",
    "SessionAnalysis",
    # Session management
    "InterviewSessionManager",
    # Output
    "AnalysisOutputWriter",
    # Agent
    "InterviewAnalyzer",
    "InterviewAnalysisOutput",
    "create_interview_analyzer",
]

__version__ = "0.1.0"
