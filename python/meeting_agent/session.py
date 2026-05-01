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
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .models import (
    ActionItem,
    AlfredExtraction,
    ChatMessage,
    Decision,
    InterviewSession,
    MeetingEvent,
    OpenQuestion,
    OutboundChatIntent,
    Risk,
    SpeakerMapping,
    TranscriptEvent,
)


__all__ = ["InterviewSessionManager", "SessionRegistry"]


logger = logging.getLogger(__name__)


CONSOLIDATION_WINDOW_SECONDS = 2.25
SHORT_FRAGMENT_WORDS = 5
DOMINANT_SPEAKER_MIN_CHARS = 24
DOMINANT_SPEAKER_MIN_TURNS = 2
DOMINANT_SPEAKER_SHARE = 0.68
MAX_OUTBOUND_INTENTS = 24
BOT_ECHO_WINDOW_SECONDS = 180


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


def _build_prompt_cache_key(session_id: str) -> str:
    """Create a stable prompt-cache key for one meeting session."""
    return f"alfred:{session_id}"


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
    
    def start_session(
        self,
        candidate_name: str,
        meeting_url: str,
        chat_thread_id: str | None = None,
    ) -> InterviewSession:
        """
        Initialize a new interview session.

        Creates a new session with a unique ID and stores candidate information.
        Any existing session is implicitly ended.

        Args:
            candidate_name: Name of the candidate being interviewed.
            meeting_url: Teams meeting join URL.
            chat_thread_id: Teams chat thread id of the meeting (becomes the
                URL key the UI routes on, and is persisted as
                ``graph_chat_thread_id``).

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
            prompt_cache_key=_build_prompt_cache_key(session_id),
            graph_chat_thread_id=chat_thread_id,
        )
        # Seed conversation_reference_id from chat_thread_id so voice-only
        # or early-transcript sessions can post chat via the C# bot before
        # any chat activity arrives to carry the reference.
        if chat_thread_id:
            self._session.conversation_reference_id = chat_thread_id
        self._speaker_roles = {}

        logger.info(
            "Started session %s for candidate '%s' (chat_thread_id=%s)",
            session_id,
            candidate_name,
            chat_thread_id,
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

        valid_roles = {"candidate", "interviewer", "participant", "bot"}
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

    def _role_for_chat_sender(self, sender_id: str | None, from_bot: bool) -> str:
        if from_bot:
            return "bot"
        if sender_id:
            return self.get_speaker_role(sender_id) or "participant"
        return "participant"

    def _append_meeting_event(self, event: MeetingEvent) -> MeetingEvent:
        """Append to the canonical meeting ledger with light speech consolidation."""
        if self._session is None:
            raise ValueError("No active session. Call start_session() first.")

        if event.kind == "speech" and self._session.meeting_events:
            previous = self._session.meeting_events[-1]
            previous_turn = {
                "speaker_id": previous.speaker_id,
                "role": previous.role,
                "text": previous.text,
                "timestamp": previous.timestamp_utc,
            }
            current_turn = {
                "speaker_id": event.speaker_id,
                "role": event.role,
                "text": event.text,
                "timestamp": event.timestamp_utc,
            }
            if previous.kind == "speech" and _should_merge_turn(previous_turn, current_turn):
                previous.text = _merge_transcript_text(previous.text, event.text)
                previous.timestamp_utc = event.timestamp_utc
                previous.confidence = event.confidence or previous.confidence
                if previous.role == "unknown" and event.role != "unknown":
                    previous.role = event.role
                    previous.speaker_id = event.speaker_id
                    previous.display_name = event.display_name
                return previous

        self._session.meeting_events.append(event)
        return event

    def get_latest_meeting_event(self) -> Optional[MeetingEvent]:
        if self._session is None or not self._session.meeting_events:
            return None
        return self._session.meeting_events[-1]

    def get_recent_events_since_cursor(self, count: Optional[int] = None) -> list[MeetingEvent]:
        """Return meeting events added after the last processed agent cursor."""
        if self._session is None:
            return []
        events = self._session.meeting_events[self._session.latest_agent_cursor :]
        if count is not None and len(events) > count:
            return events[-count:]
        return list(events)

    def mark_agent_progress(self, event_id: str | None = None, latest_response_id: str | None = None) -> None:
        """Advance the agent cursor after a successful live-turn analysis."""
        if self._session is None:
            return

        if event_id:
            for index, event in enumerate(self._session.meeting_events, start=1):
                if event.event_id == event_id:
                    self._session.latest_agent_cursor = max(self._session.latest_agent_cursor, index)
                    break
        else:
            self._session.latest_agent_cursor = len(self._session.meeting_events)

        if latest_response_id:
            self._session.latest_response_id = latest_response_id

    def apply_extraction(self, extraction: AlfredExtraction | None) -> None:
        """Merge an AlfredExtraction's deltas into the session's rolling state."""
        if self._session is None or extraction is None:
            return

        running_summary = (extraction.running_summary or "").strip()
        if running_summary:
            self._session.running_summary = running_summary

        topics = [t.strip() for t in (extraction.topics or []) if t.strip()]
        if topics:
            self._session.topics = topics

        notes = [n.strip() for n in (extraction.notes or []) if n.strip()]
        if notes:
            self._session.notes.extend(notes)
            self._session.notes = self._session.notes[-200:]

        self._merge_decisions(extraction.decisions or [])
        self._merge_open_questions(extraction.open_questions or [])
        self._merge_action_items(extraction.action_items or [])
        self._merge_risks(extraction.risks or [])

    # Backwards-compat shim for any remaining callers of the old name.
    apply_alfred_action = apply_extraction

    def _merge_decisions(self, incoming: list[Decision]) -> None:
        if self._session is None or not incoming:
            return
        by_id = {d.id: d for d in self._session.decisions}
        for d in incoming:
            by_id[d.id] = d
        self._session.decisions = list(by_id.values())[-200:]

    def _merge_open_questions(self, incoming: list[OpenQuestion]) -> None:
        if self._session is None or not incoming:
            return
        by_id = {q.id: q for q in self._session.open_questions}
        for q in incoming:
            by_id[q.id] = q
        self._session.open_questions = list(by_id.values())[-200:]

    def _merge_action_items(self, incoming: list[ActionItem]) -> None:
        if self._session is None or not incoming:
            return
        by_id = {a.id: a for a in self._session.action_items}
        for a in incoming:
            by_id[a.id] = a
        self._session.action_items = list(by_id.values())[-200:]

    def _merge_risks(self, incoming: list[Risk]) -> None:
        if self._session is None or not incoming:
            return
        by_id = {r.id: r for r in self._session.risks}
        for r in incoming:
            by_id[r.id] = r
        self._session.risks = list(by_id.values())[-200:]

    def record_outbound_chat_intent(self, text: str, reply_to_message_id: str | None = None) -> None:
        """Remember recent outbound chat so the bot echo does not re-trigger Alfred."""
        if self._session is None or not text.strip():
            return

        self._session.outbound_chat_intents.append(
            OutboundChatIntent(
                text=text,
                normalized_text=_normalize_text(text),
                reply_to_message_id=reply_to_message_id,
            )
        )
        self._session.outbound_chat_intents = self._session.outbound_chat_intents[-MAX_OUTBOUND_INTENTS:]

    def is_expected_bot_echo(self, message: ChatMessage) -> bool:
        """Return True when an inbound bot chat matches a recent Alfred send intent."""
        if self._session is None or not message.from_bot:
            return False

        normalized_text = _normalize_text(message.text or "")
        if not normalized_text:
            return False

        message_ts = _parse_utc_timestamp(message.timestamp_utc)
        for intent in reversed(self._session.outbound_chat_intents):
            if intent.normalized_text != normalized_text:
                continue
            if intent.reply_to_message_id and intent.reply_to_message_id != message.reply_to_message_id:
                continue
            intent_ts = _parse_utc_timestamp(intent.timestamp_utc)
            if message_ts and intent_ts:
                if abs((message_ts - intent_ts).total_seconds()) > BOT_ECHO_WINDOW_SECONDS:
                    continue
            return True
        return False
    
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

        if event.event_type == "final" and event.text and event.text.strip():
            metadata = event.metadata
            self._append_meeting_event(
                MeetingEvent(
                    event_id=f"speech:{event.timestamp_utc}:{event.speaker_id or 'unknown'}",
                    kind="speech",
                    timestamp_utc=event.timestamp_utc,
                    source="teams_media",
                    text=event.text.strip(),
                    speaker_id=event.speaker_id,
                    participant_id=metadata.participant_id if metadata else None,
                    aad_object_id=metadata.aad_object_id if metadata else None,
                    media_source_id=metadata.media_source_id if metadata else None,
                    display_name=metadata.display_name if metadata else None,
                    role=self.get_speaker_role(event.speaker_id or "") or "unknown",
                    transcript_provider=metadata.provider if metadata else None,
                    confidence=event.confidence,
                    raw=metadata.raw_response if metadata else None,
                )
            )

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
        Return recent consolidated speech turns from the canonical meeting ledger.
        """
        if self._session is None:
            return []
        conversation: list[dict[str, str | None]] = []

        for event in self._session.meeting_events:
            if event.kind != "speech" or not event.text.strip():
                continue

            turn: dict[str, str | None] = {
                "speaker_id": event.speaker_id,
                "role": event.role or "unknown",
                "text": event.text.strip(),
                "timestamp": event.timestamp_utc,
            }
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
                "meeting_history": [],
                "running_summary": "",
                "topics": [],
                "notes": [],
            }
        
        conversation = self.get_recent_conversation(count=20)
        candidate_speaker_id = self.infer_candidate_speaker_id()
        timeline = self.get_unified_timeline()

        return {
            "session_active": self.is_active,
            "session_id": self._session.session_id,
            "candidate_name": self._session.candidate_name,
            "meeting_url": self._session.meeting_url,
            "started_at": self._session.started_at,
            "speaker_mappings": dict(self._speaker_roles),
            "candidate_speaker_id": candidate_speaker_id,
            "recent_conversation": conversation,
            "meeting_history": timeline,
            "chat_messages_count": len(self._session.chat_messages),
            "conversation_reference_id": self._session.conversation_reference_id,
            "graph_chat_thread_id": self._session.graph_chat_thread_id,
            "prompt_cache_key": self._session.prompt_cache_key,
            "latest_response_id": self._session.latest_response_id,
            "latest_agent_cursor": self._session.latest_agent_cursor,
            "running_summary": self._session.running_summary,
            "topics": list(self._session.topics),
            "notes": list(self._session.notes),
            "decisions": [d.model_dump() for d in self._session.decisions],
            "open_questions": [q.model_dump() for q in self._session.open_questions],
            "action_items": [a.model_dump() for a in self._session.action_items],
            "risks": [r.model_dump() for r in self._session.risks],
            "alfred_muted": self._session.alfred_muted,
            "total_events": len(self._session.transcript_events),
            "final_events": len([
                e for e in self._session.transcript_events
                if e.event_type == "final"
            ]),
        }

    def get_agent_context_snapshot(
        self,
        trigger_event: MeetingEvent | None = None,
    ) -> dict[str, Any]:
        """Return the stable Alfred context plus the newest appended events."""
        if self._session is None:
            return {
                "session_active": False,
                "stable_prefix": {},
                "dynamic_tail": [],
            }

        session_context = self.get_session_context()
        dynamic_tail = [event.model_dump() for event in self.get_recent_events_since_cursor()]
        return {
            **session_context,
            "stable_prefix": {
                "session_id": self._session.session_id,
                "candidate_name": self._session.candidate_name,
                "meeting_url": self._session.meeting_url,
                "started_at": self._session.started_at,
                "speaker_mappings": dict(self._speaker_roles),
                "prompt_cache_key": self._session.prompt_cache_key,
                "latest_response_id": self._session.latest_response_id,
                "running_summary": self._session.running_summary,
                "topics": list(self._session.topics),
                "notes": list(self._session.notes),
                "decisions": [d.model_dump() for d in self._session.decisions],
                "open_questions": [q.model_dump() for q in self._session.open_questions],
                "action_items": [a.model_dump() for a in self._session.action_items],
                "risks": [r.model_dump() for r in self._session.risks],
                "alfred_muted": self._session.alfred_muted,
            },
            "dynamic_tail": dynamic_tail,
            "trigger_event": trigger_event.model_dump() if trigger_event else None,
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

    # ------------------------------------------------------------------
    # Meeting chat (Alfred)
    # ------------------------------------------------------------------

    def add_chat_message(self, message: ChatMessage) -> None:
        """Append a meeting chat message to the session and capture the conversation ref."""
        if self._session is None:
            raise ValueError("No active session. Call start_session() first.")

        for existing in self._session.chat_messages:
            if (
                existing.message_id == message.message_id
                and existing.event_type == message.event_type
            ):
                if (
                    message.conversation_reference_id
                    and existing.conversation_reference_id is None
                ):
                    existing.conversation_reference_id = message.conversation_reference_id
                if message.raw is not None and existing.raw is None:
                    existing.raw = message.raw
                if message.html and not existing.html:
                    existing.html = message.html
                if message.text and not existing.text:
                    existing.text = message.text
                return

        self._session.chat_messages.append(message)

        if self._session.graph_chat_thread_id is None:
            self._session.graph_chat_thread_id = message.chat_thread_id

        if (
            message.conversation_reference_id
            and self._session.conversation_reference_id is None
        ):
            self._session.conversation_reference_id = message.conversation_reference_id
            logger.info(
                "Captured conversation_reference_id=%s for session %s",
                message.conversation_reference_id,
                self._session.session_id,
            )

        if message.event_type != "chat_deleted" and (message.text or "").strip():
            self._append_meeting_event(
                MeetingEvent(
                    event_id=f"chat:{message.message_id}",
                    kind="chat",
                    timestamp_utc=message.timestamp_utc,
                    source="bot_framework" if message.raw is None else "graph_notification",
                    text=(message.text or "").strip(),
                    html=message.html,
                    speaker_id=message.sender_id,
                    display_name=message.sender_display_name,
                    role=self._role_for_chat_sender(message.sender_id, message.from_bot),
                    message_id=message.message_id,
                    reply_to_message_id=message.reply_to_message_id,
                    from_bot=message.from_bot,
                    raw=message.raw,
                )
            )

    def get_unified_timeline(self, count: Optional[int] = None) -> list[dict[str, object]]:
        """
        Return the canonical meeting ledger as an ordered timeline.
        """
        if self._session is None:
            return []

        entries = [event.model_dump() for event in self._session.meeting_events]
        return entries[-count:] if count is not None and len(entries) > count else entries


# ---------------------------------------------------------------------------
# Multi-session registry
# ---------------------------------------------------------------------------


_DEFAULT_THREAD_ID = "default"


class SessionRegistry:
    """
    Registry of concurrent meeting sessions keyed by Teams chat_thread_id.

    The sink hosts one ``InterviewSessionManager`` per active meeting. New
    threads are auto-created on first ingress so the bot does not need to
    call ``/session/start`` explicitly. Closed sessions are kept in the
    registry so the UI can still poll their status until garbage collected.

    The registry also exposes a ``DEFAULT`` legacy slot used by the original
    singleton routes (``/session/start``, ``/session/status`` etc.) so the
    existing test suite and tooling keep working unchanged.
    """

    DEFAULT_THREAD_ID = _DEFAULT_THREAD_ID

    def __init__(self) -> None:
        self._managers: dict[str, InterviewSessionManager] = {}

    @property
    def thread_ids(self) -> list[str]:
        """All chat_thread_ids currently tracked (active or closed)."""
        return list(self._managers.keys())

    def active_thread_ids(self) -> list[str]:
        """chat_thread_ids that have an active session right now."""
        return [tid for tid, mgr in self._managers.items() if mgr.is_active]

    def get(self, chat_thread_id: str) -> Optional[InterviewSessionManager]:
        return self._managers.get(chat_thread_id)

    def get_or_create(self, chat_thread_id: str) -> InterviewSessionManager:
        manager = self._managers.get(chat_thread_id)
        if manager is None:
            manager = InterviewSessionManager()
            self._managers[chat_thread_id] = manager
            logger.info("Registered new session manager for chat_thread_id=%s", chat_thread_id)
        return manager

    def get_or_start(
        self,
        chat_thread_id: str,
        candidate_name: str = "Meeting",
        meeting_url: str = "",
    ) -> InterviewSessionManager:
        """Resolve manager and ensure it has an active session."""
        manager = self.get_or_create(chat_thread_id)
        if not manager.is_active:
            manager.start_session(
                candidate_name=candidate_name,
                meeting_url=meeting_url,
                chat_thread_id=chat_thread_id,
            )
        return manager

    def end(self, chat_thread_id: str) -> Optional[InterviewSession]:
        manager = self._managers.get(chat_thread_id)
        if manager is None or not manager.is_active:
            return None
        return manager.end_session()

    def discard(self, chat_thread_id: str) -> None:
        """Remove a manager entirely (used by tests for isolation)."""
        self._managers.pop(chat_thread_id, None)

    def clear(self) -> None:
        self._managers.clear()

    def resolve_default(self) -> Optional[InterviewSessionManager]:
        """
        Pick the singleton-compatible session for legacy callers.

        Used by ``/session/*`` routes that were written before per-meeting
        URL routing existed. If exactly one session is active, return it;
        otherwise prefer the explicit ``DEFAULT`` slot. Returns ``None`` if
        no manager has ever been registered.
        """
        if _DEFAULT_THREAD_ID in self._managers:
            return self._managers[_DEFAULT_THREAD_ID]
        active = [mgr for mgr in self._managers.values() if mgr.is_active]
        if len(active) == 1:
            return active[0]
        if not self._managers:
            return None
        # Fall back to most recently registered manager so legacy
        # ``/session/status`` keeps returning *something* in the singleton case.
        return next(reversed(list(self._managers.values())))

    def manager_for_session_id(self, session_id: str) -> Optional[InterviewSessionManager]:
        for manager in self._managers.values():
            if manager.session is not None and manager.session.session_id == session_id:
                return manager
        return None

    def thread_id_for_session_id(self, session_id: str) -> Optional[str]:
        for thread_id, manager in self._managers.items():
            if manager.session is not None and manager.session.session_id == session_id:
                return thread_id
        return None
