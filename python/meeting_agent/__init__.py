"""Alfred meeting-agent package."""

from .models import (
    TranscriptEvent,
    EventMetadata,
    EventError,
    SpeakerMapping,
    InterviewSession,
    MeetingEvent,
    AnalysisItem,
    SessionAnalysis,
)

from .session import InterviewSessionManager

from .output import AnalysisOutputWriter

from .agent import (
    AlfredAnalyzer,
    InterviewAnalyzer,
    InterviewAnalysisOutput,
    RunningAssessment,
    create_alfred_analyzer,
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
    "MeetingEvent",
    "AnalysisItem",
    "SessionAnalysis",
    # Session management
    "InterviewSessionManager",
    # Output
    "AnalysisOutputWriter",
    # Agent
    "AlfredAnalyzer",
    "InterviewAnalyzer",
    "InterviewAnalysisOutput",
    "RunningAssessment",
    "create_alfred_analyzer",
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
