# Alfred POC: Raw audit, identity, and proactivity uplift

## Context

Alfred today persists a normalized ledger (`meeting_events`) and a rolling
dossier, but the upstream raw stream is lossy and the agent's view of "who
said what" is structurally weak. Four problems compound:

1. **No immutable raw audit.** Partials are counted but not persisted
   (`python/transcript_sink.py:1202`); chat events outside an active session
   are dropped entirely (`python/transcript_sink.py:1357`); session-lifecycle
   transcript events are written to file but not SQLite. There is no single
   source of truth for "exactly what we received from Teams/STT."
2. **Raw and curated state share one table.** `meeting_events` is already a
   filtered view (finals + non-deleted chats, dedup'd by `message_id`). Anyone
   wanting to replay or audit cannot, and any future cleanup of fragments /
   filler in the working ledger will permanently delete information.
3. **Speakers are anonymous despite Teams telling us who they are.** Two
   sources exist today and both are discarded:
   - The Teams Media SDK gives us per-buffer `ActiveSpeakers` (uint[] of
     `MediaSourceId`s), `UnmixedAudioBuffers` (per-participant streams), and
     `DominantSpeakerChanged` events (`src/Services/CallHandler.cs:163-170`,
     `:205-214`). These are the authoritative "who is speaking" signal.
   - The Graph Communications SDK exposes `ICall.Participants`; each
     `Participant.Resource.MediaStreams[].SourceId` (MSI) is bound to that
     participant's `Info.Identity.User.Id` (AAD GUID) and display name. That
     is a direct MSI→AAD lookup table, in-band, no Graph REST call needed.
   Today the C# bot logs MSIs and throws them away; the active diarization-
   capable transcribers (Deepgram, `AzureConversationTranscriber` — both
   produce `speaker_N`) are the *only* speaker signal that reaches Python,
   so the agent sees `speaker_0` instead of "Logan". `MeetingEvent`'s
   `participant_id` / `aad_object_id` / `display_name` / `media_source_id`
   fields exist (`python/meeting_agent/models.py:101-122`) but are never
   populated, and `InterviewSession.speaker_mappings` is never written.
4. **Proactivity is implicit.** The prompt has a hard silence bias
   (`python/batcave_platform/specs/alfred.yaml:30-31`) with no explicit
   intervention rules, so Alfred is passive even when ambiguity matters. We
   want explicit policy without reintroducing a `SEND/ASK/SILENT` action enum
   — the contract stays `AlfredExtraction` + `send_to_meeting_chat`.

This plan is a POC plan. No NFR / perf / migration / cost considerations.

---

## Enhancement 1 — Append-only raw audit store

### Goal
Every inbound event from Teams media, STT, Graph notification, and Bot
Framework is captured to an append-only `raw_ingest_events` table BEFORE any
filter (partial drop, session-active drop, echo-suppression). Exposed via
read endpoints + NDJSON export.

### Schema
Add a sixth table in `python/meeting_agent/persistence.py` (alongside the
existing 5 in the DDL block at lines 46-131):

```sql
CREATE TABLE IF NOT EXISTS raw_ingest_events (
    raw_event_id          TEXT PRIMARY KEY,        -- UUIDv4 generated server-side
    session_id            TEXT,                    -- nullable; pre-session events allowed
    received_at_utc       TEXT NOT NULL,           -- server receipt time
    provider_timestamp_utc TEXT,                   -- payload's own ts (event.timestamp_utc / Graph createdDateTime)
    source                TEXT NOT NULL,           -- teams_media | stt | graph_notification | bot_framework | system
    event_type            TEXT NOT NULL,           -- partial | final | session_started | session_stopped |
                                                   -- chat_created | chat_updated | chat_deleted | error
    speaker_or_sender_id  TEXT,                    -- speaker_id for stt; sender_id (AAD) for chat
    payload_hash          TEXT NOT NULL,           -- SHA256 of raw_payload_json
    raw_payload_json      TEXT NOT NULL,           -- exact body received
    normalized_payload_json TEXT,                  -- post v1->v2 normalization or post ChatMessage build (null if dropped)
    normalized_event_id   TEXT,                    -- meeting_events.event_id when promoted; null when dropped
    dropped_reason        TEXT                     -- null | partial_transcript | session_inactive |
                                                   -- echo_suppressed | duplicate_message_id | malformed
);
CREATE INDEX IF NOT EXISTS idx_raw_ingest_session_received
    ON raw_ingest_events(session_id, received_at_utc);
CREATE INDEX IF NOT EXISTS idx_raw_ingest_payload_hash
    ON raw_ingest_events(payload_hash);
```

### Files to modify

- `python/meeting_agent/persistence.py`
  - Extend DDL (lines 46-131) with the table above.
  - Add `record_raw_ingest_event(raw: RawIngestEvent) -> None` (mirror of
    `append_meeting_event` at lines 208-234, INSERT OR REPLACE).
  - Add `get_raw_events(session_id, since=None, limit=None)` and
    `iter_raw_events(session_id)` (generator) for streaming export, mirroring
    `get_ledger` at lines 369-384.

- `python/meeting_agent/models.py`
  - Add `RawIngestEvent` Pydantic model with the fields above.
  - Extend `MeetingEvent` (lines 101-122) with `source_raw_event_ids: list[str] = []`
    so promoted events back-link to their raw origin (one-to-many: a chat
    `chat_updated` may merge into the same `meeting_events` row created by
    `chat_created`).

- `python/transcript_sink.py`
  - Add a helper `record_raw(source, event_type, request_obj, ...) -> raw_event_id`
    that hashes the payload, persists to `raw_ingest_events`, and returns the id.
  - In `receive_transcript` (lines 1146-1317): call `record_raw` immediately
    after the request lands and before `normalize_v1_to_v2`. Pass the returned
    `raw_event_id` down to `session_manager.add_transcript` so `MeetingEvent`
    gets `source_raw_event_ids=[raw_event_id]` when promoted. On partial drop
    (line 1202), still persist with `dropped_reason="partial_transcript"`.
  - In `receive_chat_message` (lines 1320-1401): call `record_raw` BEFORE the
    `session_manager.is_active` filter (line 1357). When inactive, persist
    with `dropped_reason="session_inactive"`. When active and echo-suppressed,
    persist with `dropped_reason="echo_suppressed"`. When promoted, link via
    `source_raw_event_ids`.
  - Lifecycle/error transcript events also persist (`dropped_reason=null`,
    `normalized_event_id=null` since they are not ledger items).

- New endpoints (add next to the existing history routes in the same file):
  - `GET /sessions/{id}/raw-events` → JSON list (paginated by `since` + `limit`)
  - `GET /sessions/{id}/raw-events/export.ndjson` → `StreamingResponse` of
    NDJSON, one raw event per line, ordered by `received_at_utc`. Use
    `iter_raw_events` to avoid loading the whole table.

### Notes
- Echo suppression today is checked via `is_expected_bot_echo`
  (`python/meeting_agent/session.py:490-510`) AFTER ingest; we keep that, but
  the raw row is still recorded so audit shows the bot echo was received.
- `OutboundChatIntent` lives only in memory (not persisted today); leave it
  alone — the Alfred-sourced `MeetingEvent` row + `tool_calls` row already
  audit outbound. Raw store is for INBOUND only.

---

## Enhancement 2 — Split raw audit from agent memory

### Goal
Three logical layers, all persisted, with explicit backlinks:

| Layer | Table | Mutability | Used by |
|---|---|---|---|
| Raw ingest | `raw_ingest_events` (new) | append-only, immutable | audit, replay, NDJSON export |
| Working ledger | `meeting_events` (existing) | append + cleaning allowed | agent prompt context |
| Dossier / memory | `dossier_items` + `extractions` (existing) | upsert by id | agent state, UI |

This means the working ledger is now allowed to: drop filler, merge speech
fragments into a single turn, mark events `superseded`, etc., without losing
audit fidelity — because the raw layer underneath is intact.

### Files to modify

- `python/meeting_agent/models.py`
  - `MeetingEvent`: add `source_raw_event_ids: list[str] = []` (already noted
    above for E1).
  - `MeetingEvent`: add optional `superseded_by: str | None = None` for the
    future fragment-merge use case. (Not used by the agent yet; column exists
    so we don't migrate later.)
  - Confirm `Decision`, `OpenQuestion`, `ActionItem`, `Risk` already carry
    `source_event_ids: list[str]` (they do — `models.py:144-191`). No change.

- `python/meeting_agent/persistence.py`
  - Add `source_raw_event_ids` (TEXT JSON) and `superseded_by` (TEXT) columns
    to the `meeting_events` DDL (lines 61-80).
  - Update `append_meeting_event` (lines 208-234) to write the new columns.
  - Update `get_ledger` to return them.

- `python/meeting_agent/session.py`
  - `_append_meeting_event` needs to accept and forward `source_raw_event_ids`.
  - `add_transcript` (~line 539) and `add_chat_message` (~line 870) wire the
    raw id through.

- `python/transcript_sink.py`
  - `GET /sessions/{id}/ledger` (1962-1970) already returns ledger; just
    surface the new fields in the response (no shape break — they're additive).

### Agent-prompt impact
- `_build_prompt` in `python/meeting_agent/agent.py:214-288` and
  `_format_history_line:204-212` already format from `meeting_events`. No
  prompt change needed for this enhancement — the agent continues to see the
  cleaned working ledger, exactly as today.

### Out of scope for POC
- We do **not** implement filler-pruning or fragment-merge in the working
  ledger this round. We just cement the layering and backlinks so future
  cleanup is non-destructive. This unblocks E3 (where speaker resolution may
  retroactively rewrite a `MeetingEvent.speaker_id` based on roster
  reconciliation, which now is allowed because raw is immutable).

---

## Enhancement 3 — Participant identity layer

### Goal
Use Teams as the source of truth for "who is speaking" — it already knows.
Build:

1. A persisted Teams participant roster per session, sourced from the live
   `ICall.Participants` collection (in-band, no Graph REST call needed).
2. An MSI → AAD-GUID lookup table built from
   `Participant.Resource.MediaStreams[].SourceId`, kept current as
   participants join, mute, change device, or leave.
3. Tagging every transcript event with the contemporaneous `MediaSourceId`
   so Python can resolve speaker → AAD → display name without inference.
4. STT `speaker_N` demoted to a within-MSI sub-divider — used only when a
   single MSI hosts multiple humans (Teams Rooms device), to keep utterances
   from different humans in the same room from collapsing.
5. A trusted AAD-GUID lookup from chat `sender_id` (already authoritative on
   the Graph notification path).
6. Backfill of `MeetingEvent.participant_id` / `aad_object_id` /
   `display_name` / `media_source_id` for both speech and chat events.

### C# side — capture identity in-band

- `src/Services/CallHandler.cs` — subscribe to participant updates on the
  call:
  ```csharp
  Call.Participants.OnUpdated += OnParticipantsUpdated;
  ```
  In the handler, walk added/updated participants. For each, read:
  - `participant.Resource.Info.Identity.User.Id`         (AAD GUID)
  - `participant.Resource.Info.Identity.User.DisplayName`
  - `participant.Resource.MediaStreams` (filter `MediaType == "audio"`),
    each with `SourceId` (the MSI as a string; convert to uint).
  - `participant.Resource.Info.Identity.Application?.Id` (skip the bot's own
    app id and any other app participants when building the human roster).
  Forward to Python via a new publisher method on
  `PythonTranscriptPublisher` (or a sibling `PythonParticipantsPublisher`):
  `POST /session/participants` with payload:
  ```json
  {
    "session_id": "...",
    "fetched_at_utc": "...",
    "participants": [
      { "aad_object_id": "<GUID>", "display_name": "Logan",
        "user_principal_name": "...", "media_source_ids": [12345, 67890],
        "is_in_lobby": false, "role": "presenter",
        "is_application": false }
    ]
  }
  ```
  Re-emit on every `OnUpdated` so MSI changes (mute/unmute, device switch)
  reach Python promptly.

- `src/Services/CallHandler.cs:OnAudioMediaReceived` — capture the
  contemporaneous speaker hint per buffer. Two options, both cheap:
  - `buffer.ActiveSpeakers` (uint[]) — full active set at this instant.
  - `_lastDominantSpeaker` (already tracked at line 46/165).
  Forward both as transcript-event metadata. Extend
  `TeamsMediaBot.Models.TranscriptEvent` with two new optional fields:
  ```csharp
  public uint? DominantMediaSourceId { get; init; }
  public uint[]? ActiveMediaSourceIds { get; init; }
  ```
  These travel inside the JSON `metadata` block on the existing
  `POST /transcript` call (or as top-level fields — either works because
  `TranscriptEventRequest` already accepts arbitrary `metadata`).
  Crucially: the speech transcribers run on the same audio frames they were
  pushed (see `_transcriber.PushPcm16k16bitMono` at `CallHandler.cs:198`),
  so we can either:
  - have `CallHandler` annotate the most-recent MSI on a thread-local /
    `Volatile.Read` slot that the transcribers read at publish time, or
  - have the transcribers ask `CallHandler` for the current MSI when they
    publish a transcript (cleaner — the transcribers already hold a
    `PythonTranscriptPublisher`, so plumb a `Func<uint?>` provider in).
  Pick the second option: add a `Func<uint?> getCurrentDominantSpeaker`
  parameter to both `DeepgramRealtimeTranscriber` and
  `AzureConversationTranscriber` ctors, and call it inside
  `PublishEventAsync` / `FireAndForget` (the existing sites where
  `TranscriptEvent` is constructed at
  `AzureConversationTranscriber.cs:300-309` and the Deepgram equivalent).
  `TranscriberFactory.Create` gets the lambda from `CallHandler` and
  passes it through.

### Notes on STT diarization
- Both active transcribers diarize. `AzureConversationTranscriber` is the
  GA real-time-diarization Azure path
  (`AzureConversationTranscriber.cs:23-25` cites the GA blog), normalized
  to `speaker_N`. Deepgram likewise emits `speaker_N`. The deprecated
  `AzureSpeechRealtimeTranscriber` (`AzureSpeechRealtimeTranscriber.cs:118-127`)
  is unused.
- We keep `speaker_id` on `TranscriptEvent` — it's still useful as the
  within-MSI sub-divider for the Teams Rooms case (multiple humans on one
  device). It is **not** the primary identity signal anymore.

### Python side — store + resolve

- New schema in `python/meeting_agent/persistence.py`:
  ```sql
  CREATE TABLE IF NOT EXISTS meeting_participants (
      session_id        TEXT NOT NULL,
      aad_object_id     TEXT NOT NULL,
      display_name      TEXT,
      upn               TEXT,
      is_application    INTEGER NOT NULL DEFAULT 0,   -- 1 = bot/app participant, exclude from human counts
      role              TEXT,                          -- "presenter" | "attendee" | ...
      first_seen_at_utc TEXT,
      last_seen_at_utc  TEXT,
      PRIMARY KEY (session_id, aad_object_id)
  );

  CREATE TABLE IF NOT EXISTS participant_msi_bindings (
      session_id      TEXT NOT NULL,
      media_source_id INTEGER NOT NULL,
      aad_object_id   TEXT NOT NULL,
      first_seen_at_utc TEXT,
      last_seen_at_utc  TEXT,
      PRIMARY KEY (session_id, media_source_id)
  );

  CREATE TABLE IF NOT EXISTS speaker_identity_links (
      session_id      TEXT NOT NULL,
      speaker_id      TEXT NOT NULL,            -- "speaker_0", "speaker_1"
      aad_object_id   TEXT,                     -- resolved participant; null when unresolved
      display_name    TEXT,                     -- snapshot of the resolved name (incl. "(group)" suffix)
      confidence      REAL,                     -- 0..1
      method          TEXT,                     -- manual | teams_msi_unique | teams_msi_group |
                                                -- sole_human | unresolved
      last_dominant_msi INTEGER,                -- most recent MSI that produced this binding
      updated_at_utc  TEXT,
      PRIMARY KEY (session_id, speaker_id)
  );
  ```

- `python/meeting_agent/models.py`:
  - New `Participant` and `SpeakerIdentityLink` models.
  - Extend `InterviewSession` with `participants: list[Participant]` (replaces
    the unused `speaker_mappings` cosmetic field — keep that field for
    backwards compat but populate the new one).

- New module `python/meeting_agent/identity.py` — `ParticipantResolver`:
  - `upsert_participants(session_id, participants)` — handle the
    `POST /session/participants` payload. For each participant, upsert into
    `meeting_participants` and replace MSI bindings: clear any existing
    `media_source_id → aad` rows for those MSIs that no longer point at
    this AAD, then insert the new ones.
  - `resolve_chat_sender(chat: ChatMessage) -> Participant | None` — direct
    AAD GUID lookup against `meeting_participants` (trusted,
    `method="chat_aad"`, confidence=1.0).
  - `resolve_speech(session_id, speaker_id, dominant_msi, active_msis) -> SpeakerIdentityLink`
    — called from `receive_transcript` for every final speech event:
    1. **manual** (highest priority): a row in `speaker_identity_links` with
       `method="manual"` for this `(session_id, speaker_id)` always wins.
       Written by `POST /sessions/{id}/speaker-mapping` from the UI or a
       future "voice intros" flow.
    2. **teams_msi_unique**: if `dominant_msi` (or the only entry in
       `active_msis`) maps to exactly one human AAD in
       `meeting_participants`, bind `speaker_id → that AAD`, confidence=1.0,
       `method="teams_msi_unique"`. This is the dominant case for desktop /
       mobile Teams clients — Teams gives us the answer, we just record it.
    3. **teams_msi_group**: if `dominant_msi` maps to an AAD that has been
       observed to host >1 distinct `speaker_id` values in this session, OR
       the participant is flagged as `is_application=false` but the
       participant has `display_name` matching a Teams-Rooms naming pattern
       (`Conf Room *`, `*-Room`, `*MTR*`) — treat as a shared device.
       Bind speaker_id to the device participant with
       `display_name="<participant.display_name> (group)"`,
       `method="teams_msi_group"`, confidence=0.6. The agent prompt sees
       "Conf Room A (group): ..." and treats it as "someone in Conf Room A".
    4. **sole_human**: if no MSI was carried on the transcript event but the
       session roster has exactly one human attendee (excluding the bot app
       id and any application participants), bind every `speaker_N` to that
       attendee, confidence=0.85, `method="sole_human"`. Useful for the
       Azure Speech path if MSI plumbing fails for any reason; primary path
       should normally be `teams_msi_unique`.
    5. Otherwise persist with `aad_object_id=null`,
       `display_name="Unidentified speaker"`, `method="unresolved"`,
       confidence=0.0. The agent still gets the raw `speaker_N` so it can
       at least keep two unknowns straight.
  - Persisted results live in `speaker_identity_links`. When a higher-method
    answer arrives (e.g. `teams_msi_unique` after a previous `sole_human`),
    historical `meeting_events` rows for that session+speaker_id are
    UPDATEd to backfill `aad_object_id` / `display_name` /
    `participant_id` / `media_source_id`. This is allowed because the raw
    audit layer (E1/E2) is immutable underneath.
  - **Out of scope for POC**: voice fingerprinting and the "voice intros"
    flow that would let Alfred ask each participant to say their name. The
    `manual` mapping endpoint is the seam where that lands later.

- `python/transcript_sink.py`:
  - New `POST /session/participants` handler (consumes the C#-published
    payload above; calls `ParticipantResolver.upsert_participants`).
  - New `POST /sessions/{id}/speaker-mapping` handler for manual overrides.
  - Both also write to `raw_ingest_events` (E1) with
    `source="teams_media"` (for the SDK-sourced participants payload) or
    `"system"` (for manual mapping) and appropriate `event_type`
    (`participants_updated`, `manual_speaker_mapping`).
  - Extend `TranscriptEventRequest` (currently around lines 223-261) with
    optional `dominant_media_source_id: int | None` and
    `active_media_source_ids: list[int] | None`. These come from the C#
    transcribers as described above. Pre-existing `metadata` dict can also
    carry them — keep both for forward/backward compatibility on the wire.
  - In `receive_chat_message` (1320-1401), after building `ChatMessage`, call
    `resolver.resolve_chat_sender` and populate `MeetingEvent.aad_object_id`
    / `display_name` / `participant_id` before persistence.
  - In `receive_transcript` (1146-1317), after building the speech
    `MeetingEvent`, call `resolver.resolve_speech(session_id, speaker_id,
    dominant_media_source_id, active_media_source_ids)` and populate
    `aad_object_id` / `display_name` / `participant_id` /
    `media_source_id` from the result. Persist the link to
    `speaker_identity_links`. When the resolved method strictly improves
    over a prior link for this `(session_id, speaker_id)` (priority order:
    `manual > teams_msi_unique > teams_msi_group > sole_human > unresolved`),
    issue an UPDATE on historical `meeting_events` rows for that
    speaker_id to backfill the new identity.
  - New `GET /sessions/{id}/participants` and
    `GET /sessions/{id}/speaker-identity` for UI / debug.

- `python/meeting_agent/agent.py:_format_history_line:204-212`: when an event
  has `display_name`, render `"Logan: ..."`; fall back to `"speaker_0: ..."`
  when no resolution. Add a one-liner to the prompt's stable prefix that
  enumerates known participants (`get_agent_context_snapshot`) so the LLM has
  the roster as context.

### Notes
- Both active STT providers diarize — Deepgram via
  `DeepgramRealtimeTranscriber` and Azure via the GA-real-time-diarization
  `AzureConversationTranscriber` (`AzureConversationTranscriber.cs`). Both
  emit `speaker_N`. The deprecated `AzureSpeechRealtimeTranscriber` (no
  diarization) is unused.
- The primary identity signal is now Teams' own MSI→AAD mapping carried
  on every transcript event; STT `speaker_N` is the within-MSI sub-divider.
- We are not solving voice fingerprinting; we are reconciling identifiers
  Teams already provides.

---

## Enhancement 4 — Explicit proactivity policy (no action enum)

### Goal
Encode the four intervention rules + a cooldown in `alfred.yaml`, surface
them in the prompt's stable block, and (for the cooldown only) enforce
server-side. Contract stays `AlfredExtraction` + `send_to_meeting_chat`.

### Spec change
- `python/batcave_platform/specs/alfred.yaml` — add under `agent:`:
  ```yaml
  agent:
    intervention_policy:
      cooldown_seconds: 45                # min gap between Alfred posts unless directly addressed
      directly_addressed_bypass: true     # if a human @mentions or names Alfred, skip cooldown
      rules:
        - id: missing_owner
          when: action_item.owner is null
          ask: "Who's owning this one?"
        - id: missing_due
          when: action_item.due is null and action_item.status == "owned"
          ask: "When do we need this by?"
        - id: implied_decision
          when: decision.status == "tentative" and last_seen_age_seconds > 60
          ask: "Are we calling that decided?"
        - id: unresolved_disagreement
          when: two participants disagreed on a decision and the topic moved on
          ask: "Did we settle <topic>?"
  ```

### Spec model change
- `python/batcave_platform/spec_models.py:54-71` (`AgentSpec`): add
  ```python
  intervention_policy: dict[str, Any] | None = None
  ```
  The loader at `python/batcave_platform/spec_loader.py:69` deserializes
  through Pydantic — no loader change.

### Prompt change
- `python/meeting_agent/agent.py:_build_prompt:214-288`: render an
  `## Intervention Rules` block in the **stable** prefix (so it caches), built
  from `intervention_policy.rules`. Plain text, e.g.:
  ```
  ## When to break silence (call send_to_meeting_chat)
  You SHOULD ask when:
    - an action item exists without an owner
    - an owned action item has no due date
    - a decision is tentative for >60s of conversation
    - two participants disagreed and the topic moved on without resolution
  Cooldown: do not call the tool more than once every 45 seconds unless a
  human directly addresses you (mentions "Alfred").
  ```
- Crucially: keep the existing "strong bias toward silence" line. The new
  block is "strong bias toward silence, **except** when these conditions are
  met." The agent contract is unchanged.

### Tool change (cooldown enforcement)
- `python/meeting_agent/tools.py:send_to_meeting_chat_impl` (around line 139,
  after the mute check, before the post): read
  `session.outbound_chat_intents[-1].timestamp_utc`. If the gap is less than
  `cooldown_seconds`, return
  `SendResult(ok=False, reason="cooldown_active")` without posting. The
  failure flows back into the LLM context on the next tick (per existing
  `SendResult` contract at `tools.py:87-93`), so the model sees its own
  attempt was rate-limited.
- Bypass: if the trigger event was a human chat or speech turn whose text
  contains a configured mention string (e.g. `"alfred"` case-insensitive,
  pulled from `intervention_policy.directly_addressed_bypass`), skip the
  cooldown. Implement by passing the trigger event into
  `AlfredAgentContext` (already plumbed) and inspecting it inside the tool.

### Tests
- `python/tests/test_tools.py` — add:
  - `test_tool_refuses_when_cooldown_active`
  - `test_tool_allows_when_directly_addressed_bypasses_cooldown`
- New `python/tests/test_intervention_policy.py`:
  - `test_prompt_includes_intervention_rules_block` — asserts the rendered
    prompt from `_build_prompt` contains the four rule strings when the spec
    has a policy.
  - `test_agent_asks_when_action_item_has_no_owner` — feed a context with
    one ownerless `ActionItem` and assert (a) the analyzer ran and
    (b) `tool_records` contains a `send_to_meeting_chat` call. Use the
    existing pattern from `test_tools.py` for `AlfredAgentContext` setup.
  - `test_agent_stays_silent_on_normal_flow` — control case (no
    triggers).

### Explicit non-goals
- No `SEND/ASK/SILENT` enum (rule 4 of `ALFRED.md:585-602`).
- No new agent contract — still `AlfredExtraction` + `send_to_meeting_chat`.
- No deterministic server-side rule evaluator that fires the tool itself —
  the LLM remains the decision-maker, the rules just steer its prior.

---

---

## Enhancement 5 — Per-meeting sink routing

### Goal
Replace the global "one configured sink for the whole bot process" wiring
with a per-call sink override carried on the join request. Each meeting
can target its own Python sink (e.g. one sink per customer / team), with
the existing `TranscriptSink` config block as the fallback default for
auto-invite flows that don't carry an override.

### Current state (verified)

- `src/Program.cs:52` loads `TranscriptSinkConfiguration` from the root
  `TranscriptSink` config key once at startup.
- `src/Config/appsettings.example.json:44-46` defines the two endpoints
  (`PythonEndpoint`, `ChatEndpoint`) on that block.
- `src/Program.cs:86` registers `PythonChatPublisher` via
  `AddHttpClient<PythonChatPublisher>()`. The publisher reads
  `_config.ChatEndpoint` on every `PublishAsync` call
  (`PythonChatPublisher.cs:39-69`) — a single URL for every meeting.
- `src/Services/PythonTranscriptPublisher.cs` is constructed directly
  inside `TranscriberFactory.Create` (`TranscriberFactory.cs:60-61`) using
  `_pythonEndpoint` captured at factory construction — also a single URL
  for every meeting.
- `src/Controllers/CallingController.cs:313-319` defines
  `JoinMeetingRequest { JoinUrl, ... }`. The auto-invite path inside
  `TeamsCallingBotService` (e.g. when the bot is added to a meeting via
  Graph) goes through `JoinMeetingAsync` directly without traversing the
  HTTP controller.
- `src/Services/MeetingChatService.cs` and
  `src/Controllers/GraphNotificationController.cs` know
  `chatThreadId → callId` already; the gap is `callId → sinkUrl`.

### Files to modify

#### 1. Per-call override DTO + store

- New record `src/Services/PerCallSinkOverride.cs`:
  ```csharp
  public sealed record PerCallSinkOverride(
      string TranscriptEndpoint,    // e.g. "https://team-acme-sink.example.com/transcript"
      string ChatEndpoint,          // e.g. "https://team-acme-sink.example.com/chat"
      string ParticipantsEndpoint,  // e.g. "https://team-acme-sink.example.com/session/participants"
      string? AuthToken             // optional bearer; passed verbatim as Authorization header
  );
  ```
  These are full URLs (not a base + suffixes) so the override can target
  arbitrary path layouts. The join request can supply just a `sinkBaseUrl`
  and the controller derives the three endpoints from it; both shapes are
  accepted.

- New singleton `src/Services/CallSinkOverrideStore.cs` — a thread-safe
  `ConcurrentDictionary<string callId, PerCallSinkOverride>` with:
  ```csharp
  void Set(string callId, PerCallSinkOverride? overrideOrNull);
  PerCallSinkOverride? TryGet(string callId);
  void Remove(string callId);
  ```
  Set on call established, removed on call terminated (hook into
  `CallHandler.OnCallUpdated` at `CallHandler.cs:136-161`). Registered as
  singleton in DI so both the C# transcribers and the chat publishers can
  read it.

#### 2. Join request and command plumbing

- `src/Controllers/CallingController.cs:313-319` (`JoinMeetingRequest`):
  add optional fields:
  ```csharp
  [JsonProperty("sinkBaseUrl")]   public string? SinkBaseUrl { get; set; }
  [JsonProperty("sinkUrls")]      public SinkUrlsDto? SinkUrls { get; set; }
  [JsonProperty("sinkAuthToken")] public string? SinkAuthToken { get; set; }
  // SinkUrlsDto: { transcriptEndpoint?, chatEndpoint?, participantsEndpoint? }
  ```
  In the `JoinMeetingAsync` handler (`CallingController.cs:148-238`) build
  a `PerCallSinkOverride`:
  - if `SinkUrls` is provided, use those values (with the configured
    defaults filling any null);
  - else if `SinkBaseUrl` is provided, derive
    `{base}/transcript`, `{base}/chat`, `{base}/session/participants`;
  - else leave the override null (caller wants the default sink).

- `src/Services/TeamsCallingBotService.cs` (`JoinMeetingCommand` near line
  391, `JoinMeetingAsync` at 491): add optional
  `PerCallSinkOverride? SinkOverride` parameter on both the command and
  the method. When the call is established (the `OnCallEstablished` hook
  the service uses today, which is the same site that wires
  `CallHandler`), call `CallSinkOverrideStore.Set(callId, override)`.
  Auto-invite path (where the bot was added to a meeting without a join
  request hitting the controller) leaves the override null — the store
  miss falls back to the configured default endpoints.

#### 3. Refactor publishers to "publish to the URL the call hands me"

We use the **second** option from the user spec: keep both publishers as
singletons, but make `PublishAsync` URL-aware. Reasons: minimal DI
churn; HttpClient connection pooling stays one-per-target; works for the
auto-invite path that has no per-call scope.

- `src/Services/PythonTranscriptPublisher.cs:87-133` (`PublishAsync`):
  change signature to
  ```csharp
  Task PublishAsync(TranscriptEvent evt, string endpointUrl,
                    string? authToken, CancellationToken ct = default);
  ```
  Drop the constructor's `_endpoint` field (still keep
  `TimeoutSeconds` etc.). The factory site at
  `TranscriberFactory.cs:60-61` no longer needs `_pythonEndpoint`.

- `src/Services/PythonChatPublisher.cs:39-69` (`PublishAsync`): same shape
  ```csharp
  Task PublishAsync(ChatEventPayload payload, string endpointUrl,
                    string? authToken, CancellationToken ct = default);
  ```
  Drop `IsConfigured` (the caller decides whether to send by inspecting
  the resolved override); drop `_config.ChatEndpoint` reads.

- Both publishers, when `authToken` is non-null, set
  `request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", authToken)`.
  This is plumbing only — no token validation, rotation, or storage
  policy in scope for the POC.

#### 4. Resolve the URL at the call site

Each publisher caller has the `callId` already; resolution is a single
`store.TryGet(callId) ?? defaults`:

- `src/Services/AzureConversationTranscriber.cs:300-323`
  (`PublishEventAsync`) and the parallel Deepgram site: take a
  `Func<(string transcriptEndpoint, string? authToken)>` resolver in the
  constructor (alongside the dominant-speaker `Func<uint?>` from E3).
  Resolve at publish time.

- `src/Services/TranscriberFactory.cs`: receive the `CallHandler`'s
  `callId` and the `CallSinkOverrideStore` (already in DI); construct the
  resolver lambda there:
  ```csharp
  Func<(string, string?)> resolveTranscriptUrl = () =>
  {
      var ovr = _overrideStore.TryGet(callId);
      return (ovr?.TranscriptEndpoint ?? _defaults.PythonEndpoint, ovr?.AuthToken);
  };
  ```

- `src/Services/MeetingChatService.cs` (where it calls
  `PythonChatPublisher.PublishAsync`): resolve via
  `chatThreadId → callId → store.TryGet(callId) ?? defaults`. The
  `chatThreadId → callId` lookup already exists in this service. Same
  resolver pattern for the participants payload from E3.

- `src/Controllers/GraphNotificationController.cs`: the notification
  carries the chat thread id; route through `MeetingChatService` for the
  call-id resolution as today, then publish.

#### 5. Lifecycle

- On call established (in `TeamsCallingBotService` where the
  `CallHandler` is created today): `_overrideStore.Set(callId, override)`.
- On call terminated (`CallHandler.OnCallUpdated` at
  `CallHandler.cs:156-160`, where `StopTranscriptionAsync` already runs):
  `_overrideStore.Remove(callId)`.

### Default-sink fallback

When the override is null (auto-invite, or join request without a sink
override), publishers use `TranscriptSinkConfiguration.PythonEndpoint` /
`ChatEndpoint` as today. The behavior with no override is identical to
current behavior — strict superset.

### Verification

```bash
# 1. Stand up two sinks on different ports
cd python && uv run python run_variant_sink.py --instance dev-a --port 8765 \
  --product-spec batcave_platform/specs/alfred.yaml &
cd python && uv run python run_variant_sink.py --instance dev-b --port 8865 \
  --product-spec batcave_platform/specs/alfred.yaml &

# 2. Bot config TranscriptSink.PythonEndpoint -> http://127.0.0.1:8765/transcript
#    (the default). Send a join request that overrides to sink B:
curl -sS -X POST http://localhost:5000/api/calling/join \
  -H 'Content-Type: application/json' \
  -d '{
    "joinUrl": "https://teams.microsoft.com/...",
    "sinkBaseUrl": "http://127.0.0.1:8865",
    "sinkAuthToken": "test-token"
  }'
# Expect: 200 with the join response. The CallSinkOverrideStore now has
# (callId -> http://127.0.0.1:8865/{transcript,chat,session/participants},
#  authToken=test-token).

# 3. While that call is active, drive a synthetic transcript and confirm
#    sink B receives it and sink A does not:
curl -sS http://127.0.0.1:8865/stats | jq '.events_received'   # increases
curl -sS http://127.0.0.1:8765/stats | jq '.events_received'   # unchanged

# 4. Terminate that call. Then send a second join with NO sinkBaseUrl.
#    Drive transcript; confirm sink A receives it (default fallback) and
#    sink B does not.

# 5. Auto-invite path: have the bot added to a meeting via Graph (no
#    explicit /api/calling/join). Confirm transcripts land in sink A
#    (default), since no override was registered.

# 6. Both publishers attach Authorization: Bearer test-token when the
#    override carried an authToken; smoke check:
nc -l 8866 &   # listen
# Run a join with sinkBaseUrl=http://127.0.0.1:8866 sinkAuthToken=foo;
# observe inbound HTTP request has the Authorization header.

# 7. C# tests
dotnet test
# New tests:
#  - JoinMeetingRequest deserializes sinkBaseUrl + sinkAuthToken
#  - JoinMeetingRequest with sinkUrls.transcriptEndpoint overrides only that one
#  - CallSinkOverrideStore Set/TryGet/Remove round-trips
#  - PythonTranscriptPublisher.PublishAsync targets the URL passed in,
#    not any constructor-captured URL
#  - Resolver lambda returns defaults when store has no entry for callId
```

### Explicit non-goals (POC scope)

- No allowlist of accepted sink hosts. No URL validation beyond
  `Uri.TryCreate` to reject malformed strings.
- No retry/backoff tuning, circuit breakers, or per-tenant quota.
- No auth-token storage, rotation, scoping, or audit. The token is a
  string passed verbatim as an `Authorization: Bearer` header; that's it.
- No DNS pinning, mTLS, or transport hardening.
- No multi-region routing, no observability beyond existing logs.

---

## Critical files (cross-cut)

| File | Role |
|---|---|
| `python/transcript_sink.py` | All ingress routes; needs raw-record calls + roster/dominant-speaker endpoints + raw-events read endpoints. |
| `python/meeting_agent/persistence.py` | DDL block (46-131) — add `raw_ingest_events`, `meeting_participants`, `speaker_identity_links`; extend `meeting_events`. |
| `python/meeting_agent/models.py` | Add `RawIngestEvent`, `Participant`, `SpeakerIdentityLink`; extend `MeetingEvent`; extend `InterviewSession`. |
| `python/meeting_agent/session.py` | Wire `source_raw_event_ids` through `_append_meeting_event` / `add_transcript` / `add_chat_message`. |
| `python/meeting_agent/identity.py` | NEW: resolver. |
| `python/meeting_agent/tools.py` | Cooldown + directly-addressed bypass in `send_to_meeting_chat_impl`. |
| `python/meeting_agent/agent.py` | Inject `## Intervention Rules` and roster line into stable prefix; render display names in history lines. |
| `python/batcave_platform/spec_models.py` | Add `intervention_policy` field to `AgentSpec`. |
| `python/batcave_platform/specs/alfred.yaml` | Add `intervention_policy` block. |
| `src/Services/CallHandler.cs` | Subscribe to `Call.Participants.OnUpdated`; build MSI→AAD payload from `Participant.Resource.MediaStreams[].SourceId` + `Info.Identity`; expose `GetCurrentDominantSpeaker()` lambda; keep tracking `_lastDominantSpeaker`. |
| `src/Services/AzureConversationTranscriber.cs`, `src/Services/DeepgramRealtimeTranscriber.cs` | Accept a `Func<uint?>` dominant-speaker provider; include `dominant_media_source_id` and `active_media_source_ids` in published `TranscriptEvent`. |
| `src/Services/TranscriberFactory.cs` | Plumb the dominant-speaker lambda from `CallHandler` through to both transcriber constructors. |
| `src/Services/PythonTranscriptPublisher.cs` (or sibling `PythonParticipantsPublisher`) | New `PublishParticipantsAsync` posting to `POST /session/participants`. Also: refactor `PublishAsync` to accept `endpointUrl` + `authToken` per call (E5). |
| `src/Models/TranscriptEvent.cs` (and Python `TranscriptEventRequest`) | Add `DominantMediaSourceId` (uint?) and `ActiveMediaSourceIds` (uint[]?). |
| `src/Services/PerCallSinkOverride.cs` (NEW) | Per-call `(transcriptEndpoint, chatEndpoint, participantsEndpoint, authToken?)` record. |
| `src/Services/CallSinkOverrideStore.cs` (NEW) | Singleton `ConcurrentDictionary<callId, PerCallSinkOverride>` with Set/TryGet/Remove. |
| `src/Services/PythonChatPublisher.cs` | `PublishAsync` accepts `endpointUrl` + `authToken` per call. |
| `src/Services/MeetingChatService.cs` | Resolve `chatThreadId → callId → override` before publishing chat / participants. |
| `src/Controllers/CallingController.cs` | Extend `JoinMeetingRequest` with `sinkBaseUrl` / `sinkUrls` / `sinkAuthToken`; build `PerCallSinkOverride`. |
| `src/Services/TeamsCallingBotService.cs` | Carry `PerCallSinkOverride` through `JoinMeetingCommand` / `JoinMeetingAsync`; register on call established, remove on terminated. |
| `src/Services/TranscriberFactory.cs` | Inject `CallSinkOverrideStore`; build per-call resolver lambdas for transcribers. |
| `src/Program.cs` | Register `CallSinkOverrideStore` as singleton; remove `_pythonEndpoint` capture from publisher construction. |

## Reused existing utilities

- `SessionStore.append_meeting_event` (`persistence.py:208-234`) — pattern
  for the new `record_raw_ingest_event`.
- `SessionStore.get_ledger` (`persistence.py:369-384`) — pattern for the new
  `get_raw_events` / `iter_raw_events`.
- `is_expected_bot_echo` (`session.py:490-510`) — keep as-is; just record
  raw before it runs.
- `OutboundChatIntent` (`models.py:124-130`) — already gives us
  `timestamp_utc` for cooldown computation.
- `AlfredAgentContext.tool_records` — already audits tool calls; tests use
  this to assert on intervention behavior.
- `_format_dossier_block` (`agent.py:180-202`) and `get_agent_context_snapshot`
  — extend, don't replace.
- `GraphApiClient.GetResourceAsync` (`GraphApiClient.cs:139-146`) — generic
  GET we layer the participants call on top of.

## Verification

End-to-end on a fresh sink + UI pair, no real Teams call needed:

```bash
# 1. Tests (baseline today: 97 passed, 2 skipped)
cd python && uv run pytest tests -v
# Expect: previous suite + new tests in test_tools.py and
# test_intervention_policy.py all green.

# 2. Boot the sink
cd python && uv run python run_variant_sink.py \
  --instance dev --port 8765 \
  --product-spec batcave_platform/specs/alfred.yaml

# 3. E1 verification — raw store captures partials and pre-session chat
SINK=http://127.0.0.1:8765
curl -sS -X POST $SINK/transcript -H 'Content-Type: application/json' \
  -d '{"event_type":"partial","text":"hello wor","timestamp_utc":"2026-04-30T17:00:00Z","speaker_id":"speaker_0"}'
curl -sS -X POST $SINK/chat -H 'Content-Type: application/json' \
  -d '{"chat_thread_id":"t1","message_id":"m1","sender_id":"aad-A","sender_display_name":"Alex","text":"pre-session ping","timestamp_utc":"2026-04-30T17:00:01Z","from_bot":false}'
# Both should land in raw_ingest_events with dropped_reason set; nothing in
# meeting_events yet (no session).
SID=$(curl -sS -X POST $SINK/session/start -H 'Content-Type: application/json' \
  -d '{"meeting_url":"x","candidate_name":"Demo","instance_id":"dev"}' | jq -r .session_id)
curl -sS $SINK/sessions/$SID/raw-events | jq '.[].dropped_reason' | sort -u
# Expect: "partial_transcript", "session_inactive"
curl -sS $SINK/sessions/$SID/raw-events/export.ndjson | head -3
# Expect: NDJSON, one event per line.

# 4. E2 verification — backlinks
curl -sS -X POST $SINK/transcript -H 'Content-Type: application/json' \
  -d '{"session_id":"'$SID'","event_type":"final","text":"ship by friday","timestamp_utc":"2026-04-30T17:01:00Z","speaker_id":"speaker_0"}'
curl -sS $SINK/sessions/$SID/ledger | jq '.[0].source_raw_event_ids'
# Expect: non-empty array referencing the raw row above.

# 5. E3 verification — Teams MSI→AAD path
# Simulate the C# bot publishing a participants update with one human on MSI 12345.
curl -sS -X POST $SINK/session/participants -H 'Content-Type: application/json' \
  -d '{"session_id":"'$SID'","fetched_at_utc":"2026-04-30T17:01:00Z","participants":[{"aad_object_id":"aad-A","display_name":"Alex","media_source_ids":[12345],"is_application":false}]}'
# Now post a final transcript carrying the same MSI as the dominant speaker.
curl -sS -X POST $SINK/transcript -H 'Content-Type: application/json' \
  -d '{"session_id":"'$SID'","event_type":"final","text":"sounds good","timestamp_utc":"2026-04-30T17:01:30Z","speaker_id":"speaker_0","dominant_media_source_id":12345}'
curl -sS $SINK/sessions/$SID/speaker-identity | jq
# Expect: speaker_0 -> aad-A, method=teams_msi_unique, confidence=1.0
curl -sS $SINK/sessions/$SID/ledger | jq '.[-1] | {display_name, aad_object_id, media_source_id}'
# Expect: display_name="Alex", aad_object_id="aad-A", media_source_id=12345

# Conference-room case: same MSI, two distinct STT speaker indices
curl -sS -X POST $SINK/session/participants -H 'Content-Type: application/json' \
  -d '{"session_id":"'$SID'","fetched_at_utc":"2026-04-30T17:02:00Z","participants":[{"aad_object_id":"aad-A","display_name":"Alex","media_source_ids":[12345],"is_application":false},{"aad_object_id":"room-1","display_name":"Conf Room A","media_source_ids":[42],"is_application":false}]}'
curl -sS -X POST $SINK/transcript -H 'Content-Type: application/json' \
  -d '{"session_id":"'$SID'","event_type":"final","text":"hello from the room","timestamp_utc":"2026-04-30T17:02:30Z","speaker_id":"speaker_1","dominant_media_source_id":42}'
curl -sS -X POST $SINK/transcript -H 'Content-Type: application/json' \
  -d '{"session_id":"'$SID'","event_type":"final","text":"and another voice in the same room","timestamp_utc":"2026-04-30T17:02:45Z","speaker_id":"speaker_2","dominant_media_source_id":42}'
curl -sS $SINK/sessions/$SID/speaker-identity | jq '.[] | select(.method=="teams_msi_group")'
# Expect: display_name="Conf Room A (group)", method=teams_msi_group, confidence=0.6

# Manual override for the future voice-intros flow
curl -sS -X POST $SINK/sessions/$SID/speaker-mapping -H 'Content-Type: application/json' \
  -d '{"speaker_id":"speaker_1","aad_object_id":"aad-A"}'
curl -sS $SINK/sessions/$SID/speaker-identity | jq '.[] | select(.speaker_id=="speaker_1")'
# Expect: method=manual, confidence=1.0 (overrides teams_msi_group)

# 6. E4 verification — intervention policy + cooldown
# Inspect the rendered prompt (test asserts this; smoke check):
cd python && uv run pytest tests/test_intervention_policy.py -v
# Then with a running sink, post two human chats back-to-back that should
# each trigger Alfred; assert via /sessions/{id}/tool-calls that the second
# attempt within 45s returns reason="cooldown_active".

# 7. UI smoke
cd web && npm run build
cd web && npm run dev   # http://127.0.0.1:5173
# Confirm the dossier still renders, ledger lines now show display names
# when resolved.
```

Pass criteria: existing pytest baseline + the new tests green, every
verification curl above behaves as commented, web build succeeds.
