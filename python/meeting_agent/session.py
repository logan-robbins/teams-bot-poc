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


CONSOLIDATION_WINDOW_SECONDS = 2.25
SHORT_FRAGMENT_WORDS = 5
DOMINANT_SPEAKER_MIN_CHARS = 24
DOMINANT_SPEAKER_MIN_TURNS = 2
DOMINANT_SPEAKER_SHARE = 0.68


def _format_utc_timestamp(dt: datetime) -> str:
    """
    Format a datetime as ISO 8601 UTC string with 'Z' suffix.
    
    Args:
        dt: A datetime object (should be timezone-aware UTC).
        
    Returns:
        ISO 8601 formatted string ending with 'Z'.
    """
    return dt.isoformat().replace("+00:00", "Z")


def _parse_utc_timestamp(timestamp: str | None) -> Optional[datetime]:
    """Parse ISO UTC timestamps that may use a trailing 'Z'."""
    if not timestamp:
        return None
    try:
        normalized = timestamp.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _normalize_token(token: str) -> str:
    """Normalize a token for lightweight transcript deduplication."""
    return token.strip().lower().strip(".,!?;:\"'()[]{}")


def _normalize_text(text: str) -> str:
    """Normalize transcript text for comparisons."""
    return " ".join(_normalize_token(token) for token in text.split() if _normalize_token(token))


def _merge_transcript_text(existing_text: str, new_text: str) -> str:
    """Merge adjacent transcript fragments while avoiding duplicate words."""
    existing = existing_text.strip()
    incoming = new_text.strip()

    if not existing:
        return incoming
    if not incoming:
        return existing

    existing_norm = _normalize_text(existing)
    incoming_norm = _normalize_text(incoming)

    if existing_norm == incoming_norm:
        return incoming if len(incoming) > len(existing) else existing
    if existing_norm and existing_norm in incoming_norm:
        return incoming
    if incoming_norm and incoming_norm in existing_norm:
        return existing

    existing_words = existing.split()
    incoming_words = incoming.split()
    max_overlap = min(len(existing_words), len(incoming_words), 6)

    for overlap_size in range(max_overlap, 0, -1):
        existing_tail = [_normalize_token(word) for word in existing_words[-overlap_size:]]
        incoming_head = [_normalize_token(word) for word in incoming_words[:overlap_size]]
        if existing_tail == incoming_head:
            return " ".join(existing_words + incoming_words[overlap_size:])

    return f"{existing} {incoming}".strip()


def _should_merge_turn(
    previous_turn: dict[str, str | None],
    current_turn: dict[str, str | None],
) -> bool:
    """Determine whether two adjacent turns should be collapsed."""
    previous_text = str(previous_turn.get("text") or "").strip()
    current_text = str(current_turn.get("text") or "").strip()
    if not previous_text or not current_text:
        return False

    previous_ts = _parse_utc_timestamp(previous_turn.get("timestamp"))
    current_ts = _parse_utc_timestamp(current_turn.get("timestamp"))
    if previous_ts and current_ts:
        gap_seconds = (current_ts - previous_ts).total_seconds()
        if gap_seconds < 0 or gap_seconds > CONSOLIDATION_WINDOW_SECONDS:
            return False

    previous_role = str(previous_turn.get("role") or "unknown")
    current_role = str(current_turn.get("role") or "unknown")
    if (
        previous_role != current_role
        and previous_role != "unknown"
        and current_role != "unknown"
    ):
        return False

    if previous_turn.get("speaker_id") == current_turn.get("speaker_id"):
        return True

    previous_norm = _normalize_text(previous_text)
    current_norm = _normalize_text(current_text)
    if not previous_norm or not current_norm:
        return False

    if previous_norm == current_norm:
        return True
    if previous_norm in current_norm or current_norm in previous_norm:
        return True

    previous_words = len(previous_text.split())
    current_words = len(current_text.split())
    return previous_words <= SHORT_FRAGMENT_WORDS or current_words <= SHORT_FRAGMENT_WORDS


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

    def get_recent_conversation(self, count: int = 10) -> list[dict[str, str | None]]:
        """
        Return recent final transcript turns with light consolidation applied.

        Adjacent short fragments are merged so diarization churn and STT chunking
        do not create a bubble for every partial sentence fragment.
        """
        if self._session is None:
            return []

        recent_events = self.get_recent_transcripts(
            count=max(count * 3, count),
            final_only=True,
        )
        conversation: list[dict[str, str | None]] = []

        for event in recent_events:
            if not event.text or not event.text.strip():
                continue

            role = self.get_speaker_role(event.speaker_id) if event.speaker_id else None
            turn: dict[str, str | None] = {
                "speaker_id": event.speaker_id,
                "role": role or "unknown",
                "text": event.text.strip(),
                "timestamp": event.timestamp_utc,
            }

            if conversation and _should_merge_turn(conversation[-1], turn):
                previous = conversation[-1]
                previous["text"] = _merge_transcript_text(
                    str(previous.get("text") or ""),
                    turn["text"] or "",
                )
                previous["timestamp"] = turn["timestamp"]

                previous_role = str(previous.get("role") or "unknown")
                current_role = str(turn.get("role") or "unknown")
                if previous_role == "unknown" and current_role != "unknown":
                    previous["role"] = current_role
                    previous["speaker_id"] = turn["speaker_id"]

                continue

            conversation.append(turn)

        return conversation[-count:] if len(conversation) > count else conversation

    def infer_candidate_speaker_id(self, count: int = 12) -> Optional[str]:
        """
        Infer the candidate speaker from recent turns when no mapping exists.

        This is intentionally conservative. It only returns a speaker when one
        participant clearly dominates the recent conversation.
        """
        mapped_candidate = self.get_candidate_speaker_id()
        if mapped_candidate is not None:
            return mapped_candidate

        speaker_stats: dict[str, dict[str, float]] = {}
        for turn in self.get_recent_conversation(count=count):
            speaker_id = turn.get("speaker_id")
            text = str(turn.get("text") or "").strip()
            if not speaker_id or not text:
                continue

            stats = speaker_stats.setdefault(
                speaker_id,
                {"chars": 0.0, "turns": 0.0},
            )
            stats["chars"] += len(text)
            stats["turns"] += 1

        if not speaker_stats:
            return None
        if len(speaker_stats) == 1:
            return next(iter(speaker_stats))

        dominant_speaker, dominant_stats = max(
            speaker_stats.items(),
            key=lambda item: (item[1]["chars"], item[1]["turns"]),
        )
        total_chars = sum(stats["chars"] for stats in speaker_stats.values())
        dominant_share = dominant_stats["chars"] / total_chars if total_chars else 0.0

        if (
            dominant_stats["chars"] >= DOMINANT_SPEAKER_MIN_CHARS
            and dominant_stats["turns"] >= DOMINANT_SPEAKER_MIN_TURNS
            and dominant_share >= DOMINANT_SPEAKER_SHARE
        ):
            return dominant_speaker

        return None
    
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
        
        conversation = self.get_recent_conversation(count=20)
        candidate_speaker_id = self.infer_candidate_speaker_id()

        return {
            "session_active": self.is_active,
            "session_id": self._session.session_id,
            "candidate_name": self._session.candidate_name,
            "meeting_url": self._session.meeting_url,
            "started_at": self._session.started_at,
            "speaker_mappings": dict(self._speaker_roles),
            "candidate_speaker_id": candidate_speaker_id,
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
