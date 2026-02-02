"""
Interview Analysis Agent Package.

Provides real-time interview analysis using the OpenAI Agents SDK (GPT-5).

Components:
    - InterviewAnalyzer: Main agent for analyzing candidate responses
    - ChecklistAgent: Parallel agent for tracking interview progress
    - InterviewSessionManager: Manages session state and transcript history
    - AnalysisOutputWriter: Persists analysis results to JSON files
    - AgentThoughtPublisher: Real-time pub/sub for streaming thoughts to UI
    - Models: Pydantic models for transcripts, sessions, and analysis items

Example:
    >>> from interview_agent import InterviewAnalyzer
    >>> 
    >>> analyzer = InterviewAnalyzer()  # Uses gpt-5 by default
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

Last Grunted: 02/01/2026
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
    RunningAssessment,
    create_interview_analyzer,
)

from .pubsub import (
    AgentThought,
    AgentThoughtPublisher,
    ThoughtType,
    get_publisher,
)

from .checklist import (
    ChecklistAgent,
    ChecklistUpdate,
    CHECKLIST_ITEMS,
    TOPIC_KEYWORDS,
    create_checklist_agent,
    parallel_analysis,
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
    "RunningAssessment",
    "create_interview_analyzer",
    # Pub/Sub
    "AgentThought",
    "AgentThoughtPublisher",
    "ThoughtType",
    "get_publisher",
    # Checklist
    "ChecklistAgent",
    "ChecklistUpdate",
    "CHECKLIST_ITEMS",
    "TOPIC_KEYWORDS",
    "create_checklist_agent",
    "parallel_analysis",
]

__version__ = "0.2.0"
