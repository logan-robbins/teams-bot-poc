"""
Interview Session Manager.

Manages interview session state including speaker mappings,
transcript accumulation, and context generation for the analysis agent.

Thread Safety:
    This class is NOT thread-safe. Use a single instance per thread/task,
    or wrap access with appropriate synchronization primitives if sharing
    across threads.

Last Grunted: 02/05/2026
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from .models import (
    TranscriptEvent,
    InterviewSession,
    SpeakerMapping,
)


__all__ = ["InterviewSessionManager"]


logger = logging.getLogger(__name__)


def _format_utc_timestamp(dt: datetime) -> str:
    """
    Format a datetime as ISO 8601 UTC string with 'Z' suffix.
    
    Args:
        dt: A datetime object (should be timezone-aware UTC).
        
    Returns:
        ISO 8601 formatted string ending with 'Z'.
    """
    return dt.isoformat().replace("+00:00", "Z")


class InterviewSessionManager:
    """
    Manages an interview session's state and transcript history.
    
    Responsibilities:
        - Initialize sessions with candidate info and meeting URL
        - Map speaker IDs to roles (candidate vs interviewer)
        - Accumulate transcript events
        - Generate context for the analysis agent
    
    Example:
        >>> manager = InterviewSessionManager()
        >>> manager.start_session("John Smith", "https://teams.microsoft.com/...")
        >>> manager.map_speaker("speaker_0", "interviewer")
        >>> manager.map_speaker("speaker_1", "candidate")
        >>> manager.add_transcript(transcript_event)
        >>> context = manager.get_session_context()
    """
    
    def __init__(self) -> None:
        """Initialize the session manager without an active session."""
        self._session: Optional[InterviewSession] = None
        self._speaker_roles: dict[str, str] = {}  # speaker_id -> role
        logger.debug("InterviewSessionManager initialized")
    
    @property
    def session(self) -> Optional[InterviewSession]:
        """Get the current session, if active."""
        return self._session
    
    @property
    def is_active(self) -> bool:
        """Check if there is an active session."""
        return self._session is not None and self._session.ended_at is None
    
    def start_session(self, candidate_name: str, meeting_url: str) -> InterviewSession:
        """
        Initialize a new interview session.
        
        Creates a new session with a unique ID and stores candidate information.
        Any existing session is implicitly ended.
        
        Args:
            candidate_name: Name of the candidate being interviewed.
            meeting_url: Teams meeting join URL.
            
        Returns:
            The newly created InterviewSession.
            
        Example:
            >>> session = manager.start_session("Jane Doe", "https://teams.microsoft.com/l/meetup-join/...")
            >>> print(session.session_id)
            'int_20260131_103000_a1b2c3'
        """
        # End any existing session
        if self._session is not None:
            logger.info("Ending existing session before starting new one")
            self.end_session()
        
        # Generate session ID with timestamp and random suffix
        timestamp = datetime.now(timezone.utc)
        session_id = f"int_{timestamp.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        
        self._session = InterviewSession(
            session_id=session_id,
            candidate_name=candidate_name,
            meeting_url=meeting_url,
            started_at=_format_utc_timestamp(timestamp),
        )
        self._speaker_roles = {}
        
        logger.info(
            "Started session %s for candidate '%s'",
            session_id,
            candidate_name,
        )
        return self._session
    
    def end_session(self) -> Optional[InterviewSession]:
        """
        End the current session.
        
        Marks the session as ended with the current timestamp.
        
        Returns:
            The ended session, or None if no session was active.
        """
        if self._session is None:
            logger.debug("end_session called but no active session")
            return None
        
        self._session.ended_at = _format_utc_timestamp(datetime.now(timezone.utc))
        ended_session = self._session
        # Don't clear _session yet - allow retrieval of ended session data
        
        logger.info(
            "Ended session %s (total events: %d)",
            ended_session.session_id,
            len(ended_session.transcript_events),
        )
        return ended_session
    
    def map_speaker(self, speaker_id: str, role: str, name: Optional[str] = None) -> None:
        """
        Map a speaker ID to a role in the interview.
        
        Used to identify which speaker_id from diarization corresponds to
        the candidate vs the interviewer(s).
        
        Args:
            speaker_id: The speaker ID from diarization (e.g., "speaker_0").
            role: The role - "candidate" or "interviewer".
            name: Optional display name for the speaker.
            
        Raises:
            ValueError: If no session is active or role is invalid.
            
        Example:
            >>> manager.map_speaker("speaker_0", "interviewer", "HR Manager")
            >>> manager.map_speaker("speaker_1", "candidate")
        """
        if self._session is None:
            raise ValueError("No active session. Call start_session() first.")
        
        valid_roles = {"candidate", "interviewer"}
        if role not in valid_roles:
            raise ValueError(f"Invalid role '{role}'. Must be one of: {valid_roles}")
        
        # Update internal mapping
        self._speaker_roles[speaker_id] = role
        
        # Update session's speaker mappings
        # Remove existing mapping for this speaker_id if present
        self._session.speaker_mappings = [
            m for m in self._session.speaker_mappings if m.speaker_id != speaker_id
        ]
        
        display_name = name or (self._session.candidate_name if role == "candidate" else None)
        self._session.speaker_mappings.append(
            SpeakerMapping(
                speaker_id=speaker_id,
                role=role,
                name=display_name,
            )
        )
        
        logger.info(
            "Mapped speaker %s -> %s%s",
            speaker_id,
            role,
            f" ({display_name})" if display_name else "",
        )
    
    def get_candidate_speaker_id(self) -> Optional[str]:
        """
        Get the speaker ID associated with the candidate.
        
        Returns:
            The speaker_id mapped to "candidate" role, or None if not yet mapped.
            
        Example:
            >>> manager.map_speaker("speaker_1", "candidate")
            >>> manager.get_candidate_speaker_id()
            'speaker_1'
        """
        return next(
            (sid for sid, role in self._speaker_roles.items() if role == "candidate"),
            None,
        )
    
    def get_speaker_role(self, speaker_id: str) -> Optional[str]:
        """
        Get the role for a given speaker ID.
        
        Args:
            speaker_id: The speaker ID to look up.
            
        Returns:
            The role ("candidate" or "interviewer"), or None if not mapped.
        """
        return self._speaker_roles.get(speaker_id)
    
    def add_transcript(self, event: TranscriptEvent) -> None:
        """
        Add a transcript event to the session.
        
        All events are stored, but typically only "final" events
        are used for analysis.
        
        Args:
            event: The transcript event to add.
            
        Raises:
            ValueError: If no session is active.
            
        Example:
            >>> event = TranscriptEvent(
            ...     event_type="final",
            ...     text="I have 5 years of Python experience.",
            ...     timestamp_utc="2026-01-31T10:32:00.000Z",
            ...     speaker_id="speaker_1"
            ... )
            >>> manager.add_transcript(event)
        """
        if self._session is None:
            raise ValueError("No active session. Call start_session() first.")
        
        self._session.transcript_events.append(event)
        
        if event.event_type == "final":
            logger.debug(
                "Added final transcript: speaker=%s, len=%d",
                event.speaker_id,
                len(event.text) if event.text else 0,
            )
    
    def get_recent_transcripts(
        self,
        count: int = 10,
        final_only: bool = True
    ) -> list[TranscriptEvent]:
        """
        Get the most recent transcript events.
        
        Args:
            count: Maximum number of events to return.
            final_only: If True, only return "final" events (not partials).
            
        Returns:
            List of recent transcript events, newest last.
        """
        if self._session is None:
            return []
        
        events = self._session.transcript_events
        if final_only:
            events = [e for e in events if e.event_type == "final"]
        
        return events[-count:] if len(events) > count else events
    
    def get_candidate_transcripts(
        self,
        count: Optional[int] = None,
        final_only: bool = True
    ) -> list[TranscriptEvent]:
        """
        Get transcript events from the candidate only.
        
        Args:
            count: Maximum number of events to return (None for all).
            final_only: If True, only return "final" events.
            
        Returns:
            List of candidate transcript events.
        """
        candidate_id = self.get_candidate_speaker_id()
        if candidate_id is None or self._session is None:
            return []
        
        events = [
            e for e in self._session.transcript_events
            if e.speaker_id == candidate_id
        ]
        
        if final_only:
            events = [e for e in events if e.event_type == "final"]
        
        if count is not None:
            events = events[-count:] if len(events) > count else events
        
        return events
    
    def get_session_context(self) -> dict:
        """
        Generate context dictionary for the analysis agent.
        
        Returns a structured context containing session metadata,
        speaker mappings, and recent conversation history suitable
        for use as agent context.
        
        Returns:
            Dictionary with session context for agent processing.
            
        Example:
            >>> context = manager.get_session_context()
            >>> print(context["candidate_name"])
            'John Smith'
            >>> print(len(context["recent_conversation"]))
            10
        """
        if self._session is None:
            return {
                "session_active": False,
                "candidate_name": None,
                "meeting_url": None,
                "speaker_mappings": {},
                "recent_conversation": [],
                "candidate_speaker_id": None,
            }
        
        # Get recent conversation with speaker roles annotated
        recent_events = self.get_recent_transcripts(count=20, final_only=True)
        conversation = []
        
        for event in recent_events:
            role = self.get_speaker_role(event.speaker_id) if event.speaker_id else "unknown"
            conversation.append({
                "speaker_id": event.speaker_id,
                "role": role,
                "text": event.text,
                "timestamp": event.timestamp_utc,
            })
        
        return {
            "session_active": self.is_active,
            "session_id": self._session.session_id,
            "candidate_name": self._session.candidate_name,
            "meeting_url": self._session.meeting_url,
            "started_at": self._session.started_at,
            "speaker_mappings": dict(self._speaker_roles),
            "candidate_speaker_id": self.get_candidate_speaker_id(),
            "recent_conversation": conversation,
            "total_events": len(self._session.transcript_events),
            "final_events": len([
                e for e in self._session.transcript_events 
                if e.event_type == "final"
            ]),
        }
    
    def get_last_interviewer_question(self) -> Optional[str]:
        """
        Get the most recent text from an interviewer.
        
        Useful for providing context about what question the candidate
        is responding to.
        
        Returns:
            The text of the last interviewer statement, or None.
        """
        if self._session is None:
            return None
        
        # Find all interviewer speaker IDs
        interviewer_ids = {
            sid for sid, role in self._speaker_roles.items()
            if role == "interviewer"
        }
        
        if not interviewer_ids:
            return None
        
        # Search backwards for last interviewer statement
        for event in reversed(self._session.transcript_events):
            if (
                event.event_type == "final"
                and event.speaker_id in interviewer_ids
                and event.text
            ):
                return event.text
        
        return None
