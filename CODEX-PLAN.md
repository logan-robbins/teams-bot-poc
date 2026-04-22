Execution status as of 2026-04-22

- Completed: Python Alfred hot path is rebuilt around a unified append-only meeting ledger.
- Completed: The analyzer now emits AlfredAction-only live-turn output and no longer runs interview-coach scoring logic.
- Completed: Final speech turns and human meeting-chat messages both trigger analysis.
- Completed: Session state now tracks running summary, notes, topics, prompt cache key, latest response id, and graph chat thread id.
- Completed: Output persistence now stores Alfred running state and ignores legacy score aggregation when scores are absent.
- Completed: Proactive chat send path now suppresses duplicate SEND/ASK requests and serializes per chat thread.
- Completed: Teams call lifecycle now invokes meeting-chat attach/detach hooks and the media session requests unmixed meeting audio for richer speaker metadata.
- Completed: Graph meeting-chat ingress now manages subscriptions, renews them in-process, validates/decrypts notification payloads, and forwards active-meeting chat events to the Python sink.
- Completed: Inbound meeting chat now has one authoritative runtime path: Graph notifications. Bot Framework chat handling is reduced to conversation-reference capture for outbound proactive sends.
- Completed: `ALFRED.md` has been rewritten as a clean system document for future LLM operators and now matches the implemented architecture.
- Completed: C# build verification passed with `/home/azureuser/.dotnet/dotnet build`: `0 warnings, 0 errors`.
- Completed: Python verification passed with `uv run pytest` in `/home/azureuser/workspace/projects/teams-bot-poc/python`: `78 passed, 2 skipped`.
- Pending: Full live-tenant validation is still required for end-to-end Teams meeting behavior against a tenant with the final Graph app permissions and cert wiring.

Implemented in this pass

- `python/meeting_agent/models.py`
  Added `MeetingEvent`, outbound-echo intent tracking, and Alfred session state fields.
- `python/meeting_agent/session.py`
  Made `meeting_events` the canonical shared ledger and added Alfred state/cursor helpers.
- `python/meeting_agent/agent.py`
  Replaced interview analysis with `AlfredAnalyzer` and deterministic AlfredAction prompting over shared meeting history.
- `python/transcript_sink.py`
  Replaced transcript-only debounce logic with normalized event queueing for speech + chat.
- `python/meeting_agent/output.py`
  Persisted `running_summary`, `topics`, and `notes` from Alfred actions.
- `python/variants/alfred.py`
  Updated trigger-kind detection to handle `MeetingEvent`.
- `src/Controllers/SendChatController.cs`
  Added per-thread send serialization and duplicate request suppression.
- `src/Services/TeamsCallingBotService.cs`
  Wired meeting-chat attach/detach to the call lifecycle and enabled unmixed meeting audio on the media socket.
- `src/Services/CallHandler.cs`
  Added dominant-speaker and active-speaker telemetry from the Teams media socket.
- `src/Services/MeetingChatService.cs`
  Replaced the stub with active-thread tracking, app-scoped/per-chat Graph subscription management, lifecycle handling, and renewal.
- `src/Services/GraphApiClient.cs`
  Added app-token Graph client helpers for subscription CRUD and message fetch fallback.
- `src/Services/GraphNotificationProcessor.cs`
  Added Graph webhook parsing, validation, decryption, filtering, and translation to `ChatEventPayload`.
- `src/Services/GraphNotificationCrypto.cs`
  Added resource-data decryption support for encrypted Graph notifications.
- `src/Services/GraphValidationTokenValidator.cs`
  Added validation-token verification for rich Graph notifications.
- `src/Services/AlfredBot.cs`
  Reduced inbound chat handling to conversation-reference capture when Graph ingress is configured.
- `python/meeting_agent/session.py`
  Added duplicate chat-message suppression so repeated transport deliveries do not append duplicate ledger events.
- `ALFRED.md`
  Rewrote the system map so it accurately describes the implemented Alfred product, runtime truth, invariants, flows, and operator guidance.

• Below is the implementation plan I would follow.

  The key design choices behind it are:

  - Keep the current three-process architecture: Teams transport in C#, meeting state and agent orchestration in Python, UI as observer/control plane. That already matches the
    right separation of concerns for this repo.
  - Use one meeting-scoped, append-only conversation for Alfred’s shared context, and continue it with Responses API semantics instead of rebuilding prompts from scratch each
    turn. That preserves context continuity and aligns with current guidance on GPT-5.4, previous_response_id, and prompt caching. Cache hits require exact shared prefixes,
    caching starts at 1024+ tokens, and prompt_cache_key plus 24h retention are available on supported models.
  - Do not use auto-compaction on the hot path. The current Agents SDK docs explicitly note that auto-compaction can block streaming, so compaction should happen between turns
    or during idle time only.
  - For live meeting chat, keep proactive Bot Framework send as the outbound mechanism. Graph chat send with application permissions is still migration-only
    (Teamwork.Migrate.All), not the live bot path.
  - For best possible live diarized text, use Teams speaker metadata where available. Microsoft’s current media docs say app-hosted media bots can identify active and dominant
    speakers, and the SDK exposes ActiveSpeakers and UnmixedAudioBuffer.ActiveSpeakerId.

  Plan

  1. Replace the remaining interview-coach semantics at the domain boundary.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/python/meeting_agent/agent.py
  - /home/azureuser/workspace/projects/teams-bot-poc/python/transcript_sink.py
  - /home/azureuser/workspace/projects/teams-bot-poc/python/meeting_agent/models.py
  - /home/azureuser/workspace/projects/teams-bot-poc/python/meeting_agent/output.py

  PSEUDOCODE:
  - define AlfredAnalyzer as the canonical analyzer name
  - keep legacy class name aliases temporarily only if needed for import compatibility
  - remove any prompt text that says:
    - interview
    - candidate
    - interviewer
    - coaching
    - relevance_score / clarity_score as primary outputs
  - make AlfredAction the primary structured output
  - make AnalysisItem.alfred_action mandatory for Alfred mode
  - treat relevance_score / clarity_score as deprecated legacy fields
  - stop recomputing overall interview scores in output writer for Alfred sessions
  - rename any function named around "candidate turn" to "meeting event" or "analysis event"

  2. Redefine the hot-path event model around a unified meeting ledger, not transcript-only triggers.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/python/meeting_agent/models.py
  - /home/azureuser/workspace/projects/teams-bot-poc/python/meeting_agent/session.py
  - /home/azureuser/workspace/projects/teams-bot-poc/python/transcript_sink.py

  PSEUDOCODE:
  - add MeetingEvent model:
    - event_id
    - kind = speech | chat | system
    - timestamp_utc
    - source = teams_media | bot_framework | graph_notification | alfred
    - speaker_identity fields:
      - participant_id
      - aad_object_id
      - media_source_id
      - speaker_id
      - display_name
      - role
    - content fields:
      - text
      - html
      - message_id
      - reply_to_message_id
      - from_bot
    - provenance fields:
      - transcript_provider
      - confidence
      - raw pointers
  - keep transcript_events and chat_messages only as compatibility views
  - make session own one append-only ordered ledger:
    - session.meeting_events[]
  - derive meeting_history from meeting_events directly
  - expose:
    - latest_agent_cursor
    - latest_response_id
    - prompt_cache_key
    - last_compaction_at
    - conversation_reference_id
    - graph_chat_thread_id

  3. Change analysis triggering to run on every finalized speech turn and every inbound chat message.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/python/transcript_sink.py

  PSEUDOCODE:
  - delete schedule_candidate_analysis()
  - delete queue_latest_candidate_turn()
  - create enqueue_analysis_event(event):
    - if event.kind == speech and event.event_type != final: return
    - if event.kind == chat and event.event_type == chat_deleted: return
    - if event.from_bot == true and policy says ignore outbound echo: return
    - compute dedupe_key from event type + id + text + timestamp
    - if dedupe_key already seen: return
    - push normalized MeetingEvent onto agent_queue
  - in POST /transcript:
    - persist transcript
    - append speech event to meeting ledger
    - call enqueue_analysis_event(final speech event)
  - in POST /chat:
    - persist chat
    - append chat event to meeting ledger
    - call enqueue_analysis_event(chat event)

  4. Make Alfred’s context truly shared and append-only.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/python/meeting_agent/session.py
  - /home/azureuser/workspace/projects/teams-bot-poc/python/meeting_agent/agent.py

  PSEUDOCODE:
  - define session.get_agent_context_snapshot():
    - stable_prefix:
      - system instructions from spec
      - house rules
      - role schema
      - meeting metadata
      - long-running running_summary
      - current topics
      - key decisions
      - open questions
    - dynamic_tail:
      - latest meeting events since last agent cursor
  - never rebuild from "last 4 messages"
  - never reduce context to transcript-only "recent_conversation"
  - maintain:
    - immutable prefix block
    - append-only event tail block
  - when running the agent:
    - send only new tail items plus server-managed continuation handle
    - do not replay the full meeting every turn unless continuation is lost

  5. Implement the OpenAI conversation strategy explicitly.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/python/meeting_agent/agent.py
  - /home/azureuser/workspace/projects/teams-bot-poc/python/transcript_sink.py

  PSEUDOCODE:
  - use Responses API-compatible flow through the SDK or direct client
  - meeting start:
    - session.prompt_cache_key = "alfred:<tenant>:<meeting_thread_id>"
    - session.latest_response_id = null
  - per turn:
    - if latest_response_id exists:
      - call model with previous_response_id = latest_response_id
      - input = only newly appended meeting events + any refreshed state block
    - else:
      - bootstrap full prefix + initial event tail
  - set prompt_cache_retention = "24h" when model supports it
  - preserve assistant phase values if using GPT-5.4 tool-heavy flow
  - do not combine Agents SDK session memory with previous_response_id in the same run
  - disable auto-compaction on streamed/live turns
  - run compaction only:
    - after N events
    - when idle
    - when token thresholds are exceeded
    - never inline before sending a chat clarification

  6. Build the Alfred analyzer around typed action output only.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/python/meeting_agent/agent.py
  - /home/azureuser/workspace/projects/teams-bot-poc/python/legionmeet_platform/specs/alfred.yaml
  - /home/azureuser/workspace/projects/teams-bot-poc/python/variants/alfred.py

  PSEUDOCODE:
  - Agent output schema = AlfredAction
  - analyzer input:
    - current trigger event
    - current running summary
    - current topics
    - unresolved questions
    - latest meeting events since last turn
    - mute flag
    - rate-limit state
  - analyzer output must always include:
    - action = SILENT | SEND | ASK
    - rationale
    - notes delta
    - running_summary replacement
    - topics replacement
    - chat_text when action != SILENT
  - enforce server-side validation:
    - SILENT => chat_text must be null/empty
    - SEND/ASK => chat_text required
    - ASK => must end as question or be classified as clarification
  - keep strong silence bias in prompt and in post-model guardrails

  7. Split Alfred into a fast live decider and a slower synthesis path.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/python/meeting_agent/agent.py
  - /home/azureuser/workspace/projects/teams-bot-poc/python/transcript_sink.py

  PSEUDOCODE:
  - LiveTurnAgent:
    - model default = gpt-5.4-mini
    - purpose = per-event SILENT/SEND/ASK + notes delta
    - streaming enabled
    - strict latency budget
  - SynthesisAgent:
    - model default = gpt-5.4
    - purpose = deeper running summary cleanup, agenda extraction, action item consolidation
    - can run on idle/background queue
  - LiveTurnAgent writes immediate action and state deltas
  - SynthesisAgent periodically rewrites:
    - running_summary
    - topic clustering
    - action item normalization
  - if synthesis lags, live path still functions correctly

  This split follows the current model guidance: gpt-5.4 is the default flagship for complex professional workflows, while gpt-5.4-mini is the faster strong mini model for
  high-volume workloads.

  8. Upgrade the Teams media path to use Teams-known speaker metadata, not STT-only diarization.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Services/TeamsCallingBotService.cs
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Services/CallHandler.cs
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Models/TranscriptEvent.cs
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Services/AzureConversationTranscriber.cs
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Services/DeepgramRealtimeTranscriber.cs

  PSEUDOCODE:
  - change media session config:
    - ReceiveUnmixedMeetingAudio = true
  - in audio receive callback:
    - inspect buffer.ActiveSpeakers
    - inspect unmixed buffers if SDK exposes them on event payload
    - maintain map:
      - media_source_id -> participant identity
  - subscribe to participant updates on call
  - when participant roster changes:
    - refresh map of participant.id / aad id / display name / media source ids
  - for each incoming speech chunk:
    - if unmixed speaker audio exists:
      - route speaker-specific PCM to speaker-specific STT stream
    - else:
      - keep mixed stream fallback
      - annotate transcript with active speaker candidates from Teams metadata
  - publish transcript payload with:
    - media_source_id
    - aad_object_id when resolvable
    - display_name when resolvable
    - speaker_id fallback only when identity mapping fails

  9. Use provider diarization as fallback, not primary identity truth.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Services/AzureConversationTranscriber.cs
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Services/DeepgramRealtimeTranscriber.cs
  - /home/azureuser/workspace/projects/teams-bot-poc/python/meeting_agent/session.py

  PSEUDOCODE:
  - if Teams media source mapping exists:
    - prefer Teams identity attribution
  - else if STT provider returns diarization:
    - use provider speaker labels
  - else:
    - mark speaker unknown
  - maintain reconciliation layer:
    - observed_speaker_handle -> canonical_participant_id
  - never make "candidate/interviewer" assumptions
  - speaker role taxonomy becomes:
    - organizer
    - attendee
    - bot
    - unknown
    - optional custom labels only if user maps them

  10. Complete the meeting chat subscription path with the stronger Graph shape.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Services/MeetingChatService.cs
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Controllers/GraphNotificationController.cs
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Program.cs
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Config/appsettings.example.json
  - /home/azureuser/workspace/projects/teams-bot-poc/manifest/manifest.json

  PSEUDOCODE:
  - on call established:
    - resolve onlineMeeting by joinWebUrl
    - extract onlineMeeting.chatInfo.threadId
    - cache graph_chat_thread_id on session
  - choose subscription strategy:
    - preferred if tenant model allows: appCatalogs/teamsApps/{id}/installedToChats/getAllMessages
    - fallback: /chats/{threadId}/messages
  - create subscription with:
    - includeResourceData = true
    - encryptionCertificate
    - encryptionCertificateId
    - lifecycleNotificationUrl
    - clientState
    - expiration <= 60 minutes
  - start renewal loop
  - on lifecycle notifications:
    - renew or recreate
  - on encrypted payload:
    - validate clientState
    - decrypt resourceData
    - translate chatMessage -> ChatEventPayload
    - POST to sink /chat

  Current Graph docs support resource-data notifications, 60-minute subscription lifetimes, chat-level and app-installed chat message paths, and app-scoped installed-to-chats
  subscriptions. (learn.microsoft.com (https://learn.microsoft.com/en-us/graph/teams-change-notification-in-microsoft-teams-overview))

  11. Keep Bot Framework as the outbound send plane and harden it.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Services/AlfredBot.cs
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Controllers/SendChatController.cs
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Services/ConversationReferenceStore.cs
  - /home/azureuser/workspace/projects/teams-bot-poc/python/legionmeet_platform/routes/teams_chat.py

  PSEUDOCODE:
  - continue capturing conversation references from inbound chat activity
  - persist latest valid conversation reference per thread
  - in send-chat controller:
    - reject if no conversation reference
    - reject duplicate outgoing payload ids
    - apply per-thread semaphore and rate limiting
    - support reply_to_message_id when available
  - in Python teams_chat route:
    - only dispatch when action in {SEND, ASK}
    - enforce mute
    - enforce min spacing between Alfred messages
    - downgrade to SILENT if rate-limited or conversation reference missing
    - record outbound message id in session for echo suppression

  12. Add explicit echo suppression and chat coherence rules.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/python/transcript_sink.py
  - /home/azureuser/workspace/projects/teams-bot-poc/python/meeting_agent/session.py
  - /home/azureuser/workspace/projects/teams-bot-poc/python/variants/alfred.py

  PSEUDOCODE:
  - when outbound Alfred message is sent:
    - save message fingerprint + timestamp
  - when inbound chat arrives:
    - if from_bot and matches recent outbound fingerprint:
      - append to ledger as observed echo
      - do not trigger a new live-turn analysis
  - if user directly messages Alfred in meeting chat:
    - mark trigger_kind = directed_chat
    - elevate probability of SEND/ASK response
  - if human message already asks same clarification:
    - suppress Alfred ASK
  - if human just stated same content:
    - suppress Alfred SEND

  13. Make the running context cache-stable.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/python/meeting_agent/agent.py
  - /home/azureuser/workspace/projects/teams-bot-poc/python/meeting_agent/session.py

  PSEUDOCODE:
  - construct prompt/input in this order:
    1. fixed system instructions
    2. fixed tool definitions / schemas
    3. meeting metadata block
    4. stable summarized state block
    5. append-only historical ledger or server-managed continuation
    6. newest incremental events
  - never insert volatile values near the front:
    - current time
    - counters
    - random ids
    - per-turn debug notes
  - use one stable prompt_cache_key for the meeting
  - update only tail segments each turn
  - if continuation is lost:
    - rebuild from compacted stable state + recent ledger window
    - keep ordering identical across rebuilds

  This is the part that directly addresses “additional messages appended to it without breaking the cache of the first part.” Exact shared prefix first, variable tail last is
  the current documented pattern.

  14. Make meeting start and end deterministic.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/python/transcript_sink.py
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Services/TeamsCallingBotService.cs
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Services/CallHandler.cs

  PSEUDOCODE:
  - on join:
    - create meeting session if absent
    - set prompt_cache_key
    - initialize conversation state
    - initialize participant map
    - initialize graph thread id if known
  - on call established:
    - attach chat subscription
    - start media/STT streams
  - on call removed/terminated:
    - flush pending agent work
    - end chat subscription
    - run one final synthesis pass
    - mark session ended
    - freeze meeting ledger

  15. Rework persistence so it reflects a meeting assistant, not interview analytics.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/python/transcript_sink.py
  - /home/azureuser/workspace/projects/teams-bot-poc/python/meeting_agent/output.py

  PSEUDOCODE:
  - persist:
    - meeting ledger
    - running summary history
    - topic snapshots
    - outbound Alfred actions
    - final notes
  - stop persisting:
    - interview-only aggregate scores
    - candidate/interviewer assumptions
  - write one session artifact shaped like:
    - session metadata
    - event ledger
    - analysis items
    - latest state snapshot
    - final meeting report

  16. Add compliance gate around media-derived persistence.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Services/TeamsCallingBotService.cs
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Services/CallHandler.cs
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Config/appsettings.example.json
  - /home/azureuser/workspace/projects/teams-bot-poc/ALFRED.md

  PSEUDOCODE:
  - add config:
    - RecordingCompliance.Mode = disabled | policy_recording | acknowledged_nonprod
  - before enabling persistent transcript/note writes in production:
    - require explicit compliance mode
    - if policy_recording:
      - call updateRecordingStatus(recording) before persisting derived media content
      - call updateRecordingStatus(notRecording) on stop
  - if compliance mode invalid:
    - allow transient in-memory only
    - disable file persistence and final artifact write
  - document this as a hard gate, not a warning

  Microsoft’s current Graph docs explicitly state that persisting media or data derived from it requires updateRecordingStatus first.

  17. Update the manifest and permission model to match the final transport choice.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/manifest/manifest.json
  - /home/azureuser/workspace/projects/teams-bot-poc/manifest/README.md

  PSEUDOCODE:
  - keep:
    - supportsCalling = true
    - supportsVideo = false unless video features are added
    - Calls.AccessMedia.Chat
    - Calls.JoinGroupCalls.Chat
    - TeamsActivity.Send.Chat
  - if using installed-to-chats app-scoped notifications:
    - add/readjust permissions to WhereInstalled model as needed
  - keep ChatMessage.Read.Chat only if per-chat RSC subscription path remains in use
  - document which permission maps to which ingress path
  - document per-meeting install vs tenant-scoped installed-to-chats strategy

  18. Rewrite the tests around the actual Alfred product.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/python/tests/test_sink.py
  - /home/azureuser/workspace/projects/teams-bot-poc/python/tests/test_variants.py
  - /home/azureuser/workspace/projects/teams-bot-poc/python/tests/test_product_spec.py
  - add new tests for conversation/cache/state behavior

  PSEUDOCODE:
  - remove interview-specific expectations
  - add tests:
    - final speech event triggers analysis
    - chat event triggers analysis
    - bot echo does not retrigger analysis
    - unified meeting ledger ordering is stable
    - AlfredAction validation works
    - mute forces SILENT
    - SEND/ASK require chat_text
    - previous_response_id is advanced each turn
    - prompt_cache_key remains stable for one meeting
    - compaction never runs inline on hot path
    - missing continuation causes deterministic rebuild
    - conversation reference missing downgrades to SILENT
    - Teams active speaker metadata overrides STT-only speaker labels when present

  19. Validate the end-to-end operator flow.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/ALFRED.md
  - /home/azureuser/workspace/projects/teams-bot-poc/README.md
  - scripts as needed

  PSEUDOCODE:
  - operator adds Alfred to meeting
  - bot joins meeting
  - Teams media starts streaming
  - meeting chat ingress starts
  - session is created
  - agent context is initialized once
  - each final speech turn appends to ledger and triggers LiveTurnAgent
  - each human chat message appends to ledger and triggers LiveTurnAgent
  - Alfred may:
    - remain SILENT and update notes
    - SEND concise useful message
    - ASK concise clarifying question
  - users can message Alfred in meeting chat
  - those messages become first-class ledger events
  - meeting ends
  - final synthesis runs
  - session artifact is produced

  20. Deliver the product by making the repo’s “source of truth” match the behavior.

  FILES:
  - /home/azureuser/workspace/projects/teams-bot-poc/ALFRED.md
  - /home/azureuser/workspace/projects/teams-bot-poc/python/legionmeet_platform/specs/alfred.yaml
  - /home/azureuser/workspace/projects/teams-bot-poc/python/meeting_agent/*
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Services/*
  - /home/azureuser/workspace/projects/teams-bot-poc/src/Controllers/*
  - /home/azureuser/workspace/projects/teams-bot-poc/python/tests/*

  PSEUDOCODE:
  - after implementation, ensure no remaining repo path describes Alfred as interview coach
  - ensure no hot path depends on candidate/interviewer concepts
  - ensure meeting_history is the shared context
  - ensure chat and speech are symmetric agent triggers
  - ensure outbound Teams chat is proactive Bot Framework only
  - ensure Teams-known speaker metadata is exploited before fallback diarization
  - ensure cache-preserving append semantics are the default path
