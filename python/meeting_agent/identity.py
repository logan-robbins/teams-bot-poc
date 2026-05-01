"""Participant identity resolver (Enhancement 3 in PROD.md).

Bridges three signals into one stable answer to "who is speaking":

1. The Teams participant roster — published in-band by the C# bot from
   ``ICall.Participants`` (AAD GUID + display name + bound MediaSourceIds).
2. The contemporaneous ``DominantMediaSourceId`` / ``ActiveMediaSourceIds``
   carried on each transcript event.
3. Azure ConversationTranscriber's diarization label (``speaker_N``), which
   is now demoted to a within-MSI sub-divider for the Teams Rooms case.

Resolution priority (highest first):

    manual > teams_msi_unique > teams_msi_group > sole_human > unresolved

Manual rows are written by ``POST /sessions/{id}/speaker-mapping``; everything
else flows from Teams' own roster + per-buffer MSI hints. STT diarization is
no longer the primary identity signal.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional

from .models import (
    ChatMessage,
    Participant,
    SpeakerIdentityLink,
    SpeakerResolutionMethod,
    utc_now_iso,
)
from .persistence import SessionStore

logger = logging.getLogger(__name__)

__all__ = ["ParticipantResolver", "RESOLUTION_PRIORITY"]


RESOLUTION_PRIORITY: dict[SpeakerResolutionMethod, int] = {
    "unresolved": 0,
    "sole_human": 1,
    "teams_msi_group": 2,
    "teams_msi_unique": 3,
    "chat_aad": 3,
    "manual": 4,
}


_ROOM_NAME_PATTERNS = (
    re.compile(r"\bConf(?:erence)?\s*Room\b", re.IGNORECASE),
    re.compile(r"\bRoom\b", re.IGNORECASE),
    re.compile(r"\bMTR\b", re.IGNORECASE),
    re.compile(r"-Room\b", re.IGNORECASE),
)


def _looks_like_room(display_name: Optional[str]) -> bool:
    if not display_name:
        return False
    return any(pat.search(display_name) for pat in _ROOM_NAME_PATTERNS)


class ParticipantResolver:
    """Resolves speech and chat events to Teams participants.

    Stateful: tracks per-session ``speaker_id → MSI`` observations so
    teams_msi_group can be detected (one MSI hosting multiple speaker
    indices is the conference-room signal).
    """

    def __init__(self, store: SessionStore) -> None:
        self._store = store
        # session_id -> aad_object_id -> set[speaker_id seen]
        self._aad_speakers: dict[str, dict[str, set[str]]] = {}

    def upsert_participants(
        self,
        session_id: str,
        participants: Iterable[Participant],
    ) -> None:
        """Persist a participants payload from the C# bot."""
        for p in participants:
            self._store.upsert_participant(session_id, p)

    def resolve_chat_sender(
        self,
        session_id: str,
        chat: ChatMessage,
    ) -> Optional[dict[str, Any]]:
        """Look up an inbound chat sender by AAD; trusted (confidence=1.0)."""
        if not chat.sender_id:
            return None
        # Chat sender_id may be an AAD GUID (Graph notification path) or a
        # bot-framework opaque id (28: prefix). Only the GUID path matches
        # meeting_participants.
        sender = chat.sender_id.strip()
        with self._store._connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT aad_object_id, display_name, is_application"
                " FROM meeting_participants"
                " WHERE session_id = ? AND aad_object_id = ?",
                (session_id, sender),
            ).fetchone()
            if row is None:
                return None
            return {
                "aad_object_id": row["aad_object_id"],
                "display_name": row["display_name"] or chat.sender_display_name,
                "is_application": bool(row["is_application"]),
            }

    def _record_speaker_seen(
        self,
        session_id: str,
        aad: str,
        speaker_id: str,
    ) -> int:
        """Track distinct speaker_ids seen for an AAD; return the count."""
        per_session = self._aad_speakers.setdefault(session_id, {})
        seen = per_session.setdefault(aad, set())
        seen.add(speaker_id)
        return len(seen)

    def resolve_speech(
        self,
        session_id: str,
        speaker_id: Optional[str],
        dominant_msi: Optional[int],
        active_msis: Optional[list[int]] = None,
    ) -> SpeakerIdentityLink:
        """Resolve a speech event's speaker_id to an AAD identity.

        See PROD.md §3 for the priority order. The returned link is also
        persisted to ``speaker_identity_links`` when its method strictly
        improves over the previous binding for this (session, speaker_id).
        """
        speaker_key = speaker_id or "unknown"
        candidate_msi: Optional[int] = dominant_msi
        if candidate_msi is None and active_msis and len(active_msis) == 1:
            candidate_msi = active_msis[0]

        existing_row = self._store.get_speaker_identity_link(session_id, speaker_key)
        existing_method: SpeakerResolutionMethod = (
            existing_row.get("method") if existing_row else "unresolved"
        ) or "unresolved"

        # 1. Manual is sticky — never overwritten by automatic resolution.
        if existing_method == "manual":
            return SpeakerIdentityLink(
                speaker_id=speaker_key,
                aad_object_id=existing_row["aad_object_id"],
                display_name=existing_row["display_name"],
                confidence=existing_row.get("confidence") or 1.0,
                method="manual",
                last_dominant_msi=candidate_msi,
            )

        link: Optional[SpeakerIdentityLink] = None

        # 2. Teams MSI direct match.
        if candidate_msi is not None:
            participant = self._store.get_participant_for_msi(session_id, candidate_msi)
            if participant is not None:
                aad = participant["aad_object_id"]
                count = self._record_speaker_seen(session_id, aad, speaker_key)
                display = participant["display_name"]
                if count > 1 or _looks_like_room(display):
                    link = SpeakerIdentityLink(
                        speaker_id=speaker_key,
                        aad_object_id=aad,
                        display_name=f"{display or aad} (group)",
                        confidence=0.6,
                        method="teams_msi_group",
                        last_dominant_msi=candidate_msi,
                    )
                else:
                    link = SpeakerIdentityLink(
                        speaker_id=speaker_key,
                        aad_object_id=aad,
                        display_name=display or aad,
                        confidence=1.0,
                        method="teams_msi_unique",
                        last_dominant_msi=candidate_msi,
                    )

        # 3. Sole-human fallback when MSI is missing.
        if link is None:
            roster = self._store.get_participants(session_id)
            humans = [r for r in roster if not r.get("is_application")]
            if len(humans) == 1:
                only = humans[0]
                link = SpeakerIdentityLink(
                    speaker_id=speaker_key,
                    aad_object_id=only["aad_object_id"],
                    display_name=only.get("display_name") or only["aad_object_id"],
                    confidence=0.85,
                    method="sole_human",
                    last_dominant_msi=candidate_msi,
                )

        # 4. Unresolved.
        if link is None:
            link = SpeakerIdentityLink(
                speaker_id=speaker_key,
                aad_object_id=None,
                display_name="Unidentified speaker",
                confidence=0.0,
                method="unresolved",
                last_dominant_msi=candidate_msi,
            )

        # Only persist + backfill when the new method is strictly better than
        # the existing one (or there isn't one yet).
        new_priority = RESOLUTION_PRIORITY.get(link.method, 0)
        old_priority = RESOLUTION_PRIORITY.get(existing_method, 0)
        if new_priority > old_priority or existing_row is None:
            self._store.upsert_speaker_identity_link(session_id, link)
            if link.aad_object_id:
                count = self._store.backfill_meeting_event_identity(
                    session_id,
                    speaker_key,
                    aad_object_id=link.aad_object_id,
                    display_name=link.display_name,
                    media_source_id=link.last_dominant_msi,
                )
                if count:
                    logger.info(
                        "Backfilled %d ledger row(s) with identity %s -> %s",
                        count,
                        speaker_key,
                        link.display_name,
                    )

        return link

    def set_manual_mapping(
        self,
        session_id: str,
        speaker_id: str,
        aad_object_id: str,
    ) -> SpeakerIdentityLink:
        """Write a manual override and backfill the working ledger."""
        with self._store._connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT display_name FROM meeting_participants"
                " WHERE session_id = ? AND aad_object_id = ?",
                (session_id, aad_object_id),
            ).fetchone()
            display = row["display_name"] if row else aad_object_id

        link = SpeakerIdentityLink(
            speaker_id=speaker_id,
            aad_object_id=aad_object_id,
            display_name=display,
            confidence=1.0,
            method="manual",
            last_dominant_msi=None,
            updated_at_utc=utc_now_iso(),
        )
        self._store.upsert_speaker_identity_link(session_id, link)
        self._store.backfill_meeting_event_identity(
            session_id,
            speaker_id,
            aad_object_id=aad_object_id,
            display_name=display,
            media_source_id=None,
        )
        return link
