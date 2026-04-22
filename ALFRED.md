# ALFRED.md

This file is the fastest way for an LLM to become operationally useful in this
repo. It describes the current Alfred system as implemented on
`feat/alfred-chat-modality`.

Read this before editing code.

## 1. Product definition

Alfred is a Microsoft Teams meeting assistant.

The product behavior is:

1. Alfred joins a Teams meeting.
2. Live meeting audio is transcribed with speaker-aware metadata when available.
3. Meeting chat is ingested live.
4. Speech and chat are merged into one append-only meeting ledger.
5. An agent observes that shared ledger and emits one structured action:
   - `SILENT`
   - `SEND`
   - `ASK`
6. `SEND` and `ASK` can post into the meeting chat.
7. Users can message Alfred in the meeting chat; those messages are also appended
   to the same ledger and become agent input.

The core product requirement is shared context:

- Alfred must reason over one meeting-scoped history.
- New speech turns and new chat messages are appended to that history.
- The prompt prefix should stay stable enough for prompt caching and
  conversation continuation.

## 2. Current truth

These statements reflect the current branch and should be treated as source of
truth when working on the system:

- The canonical runtime state lives in the Python sink.
- The canonical meeting history is `InterviewSession.meeting_events` even though
  the type name still says `InterviewSession` for compatibility.
- Inbound meeting chat has one authoritative path in normal operation:
  Microsoft Graph notifications.
- Bot Framework is still used for one thing only:
  capturing `ConversationReference` values so Alfred can send proactive chat
  messages back into the meeting.
- The agent contract is `AlfredAction`, not interview scoring.
- Final speech turns and human chat messages both trigger agent analysis.

## 3. Architecture

There are three runtime pieces.

### 3.1 C# Teams transport

Location: `src/`

Responsibilities:

- Join Teams meetings with the Graph Communications SDK.
- Receive Teams media.
- Stream audio to the configured STT provider.
- Capture Teams meeting chat via Graph change notifications.
- Capture `ConversationReference` values from Bot Framework activities.
- Send Alfred chat messages proactively through Bot Framework.
- Forward transcript and chat events to the Python sink.

Important files:

- `src/Program.cs`
- `src/Services/TeamsCallingBotService.cs`
- `src/Services/CallHandler.cs`
- `src/Services/MeetingChatService.cs`
- `src/Services/GraphApiClient.cs`
- `src/Services/GraphNotificationProcessor.cs`
- `src/Services/GraphNotificationCrypto.cs`
- `src/Services/GraphValidationTokenValidator.cs`
- `src/Services/AlfredBot.cs`
- `src/Controllers/GraphNotificationController.cs`
- `src/Controllers/SendChatController.cs`
- `src/Services/PythonTranscriptPublisher.cs`
- `src/Services/PythonChatPublisher.cs`

### 3.2 Python sink

Location: `python/`

Responsibilities:

- Own the active meeting session state.
- Normalize transcript and chat into one append-only ledger.
- Build Alfred agent context.
- Run the live-turn analyzer.
- Apply `AlfredAction` state updates.
- Route `SEND` and `ASK` intents back to the C# bot.
- Persist session artifacts.

Important files:

- `python/transcript_sink.py`
- `python/meeting_agent/models.py`
- `python/meeting_agent/session.py`
- `python/meeting_agent/agent.py`
- `python/meeting_agent/output.py`
- `python/variants/alfred.py`
- `python/legionmeet_platform/specs/alfred.yaml`
- `python/legionmeet_platform/routes/teams_chat.py`

### 3.3 Streamlit observer UI

Location: `python/streamlit_ui.py`

Responsibilities:

- Show the unified meeting timeline.
- Show running notes and related session output.
- Let an operator inspect the live system state.

It is an observer and operator surface. It is not the source of truth.

## 4. End-to-end flows

### 4.1 Speech flow

1. `TeamsCallingBotService` joins the meeting and creates the media session.
2. `CallHandler` receives audio buffers from Teams.
3. Audio is sent to the configured realtime transcriber.
4. Transcript events are POSTed to the Python sink `/transcript`.
5. The sink stores transcript compatibility events and appends normalized
   `MeetingEvent(kind="speech")` items into `meeting_events`.
6. Final speech turns are queued for Alfred analysis.

### 4.2 Inbound meeting chat flow

1. `MeetingChatService` tracks active meeting chat thread ids from live calls.
2. It creates and renews Graph subscriptions.
3. Microsoft Graph POSTs notifications to
   `src/Controllers/GraphNotificationController.cs`.
4. `GraphNotificationProcessor` validates the batch, handles lifecycle events,
   decrypts resource data when present, and resolves/fetches message payloads
   when needed.
5. Valid chat messages are translated to `ChatEventPayload`.
6. `PythonChatPublisher` POSTs them to the Python sink `/chat`.
7. The sink appends normalized `MeetingEvent(kind="chat")` items into
   `meeting_events`.
8. Human chat messages are queued for Alfred analysis.

### 4.3 Outbound Alfred chat flow

1. The Python sink runs the analyzer on a trigger event.
2. If the result is `SEND` or `ASK`, the route layer posts to
   `src/Controllers/SendChatController.cs`.
3. `SendChatController` resolves the cached `ConversationReference`.
4. It sends the message into the Teams meeting chat through
   `CloudAdapter.ContinueConversationAsync`.
5. Outbound echo suppression in the Python session layer prevents Alfred from
   re-triggering itself on its own message.

### 4.4 Bot Framework role

`AlfredBot` is no longer the authoritative inbound meeting chat ingestion path
when Graph chat ingress is configured.

Its primary job is now:

- capture and refresh `ConversationReference` values for a meeting chat thread
- optionally forward inbound chat only when Graph ingress is not configured

That distinction matters. Do not accidentally rebuild a dual-ingestion model.

## 5. Canonical state model

The key state object is `InterviewSession` in
`python/meeting_agent/models.py`.

Important fields:

- `meeting_events`
- `transcript_events`
- `chat_messages`
- `conversation_reference_id`
- `graph_chat_thread_id`
- `prompt_cache_key`
- `latest_response_id`
- `latest_agent_cursor`
- `running_summary`
- `topics`
- `notes`
- `alfred_muted`
- `outbound_chat_intents`

Interpretation:

- `meeting_events` is the canonical append-only ledger.
- `transcript_events` and `chat_messages` are compatibility views and raw-ish
  storage, not the primary reasoning surface.
- `prompt_cache_key` and `latest_response_id` are the cache/continuation hooks
  for Alfred’s model interaction.
- `running_summary`, `topics`, and `notes` are Alfred’s rolling state.

### 5.1 MeetingEvent

`MeetingEvent` is the normalized unit Alfred reasons over.

Kinds:

- `speech`
- `chat`
- `system`

Sources:

- `teams_media`
- `graph_notification`
- `bot_framework`
- `alfred`
- `system`

The invariant is simple:

- if a new user-visible event matters to Alfred, it should become a
  `MeetingEvent`

## 6. Agent contract

The live-turn analyzer emits `AlfredAction`.

`AlfredAction` fields:

- `action`
- `rationale`
- `chat_text`
- `mentions`
- `reply_to_message_id`
- `notes`
- `running_summary`
- `topics`

Allowed actions:

- `SILENT`
- `SEND`
- `ASK`

Operational rules:

- `SILENT` must not send chat.
- `SEND` and `ASK` require `chat_text`.
- `ASK` should be an actual clarifying question.
- The system is intentionally biased toward silence.

The sink applies `AlfredAction` back into the session, updates rolling state,
records outbound intent metadata, and routes send actions to Teams chat.

## 7. Prompt and context strategy

The repo is structured around one meeting-scoped shared context.

The intended model behavior is:

- stable system/product prefix
- stable meeting metadata and running state
- append-only meeting ledger tail
- only new events added on each turn

Implementation hooks already present:

- `prompt_cache_key`
- `latest_response_id`
- `latest_agent_cursor`
- `session.get_agent_context_snapshot(...)`

When editing this area, preserve these invariants:

- do not rebuild Alfred from a tiny recent window
- do not split transcript and chat into separate agent contexts
- do not insert volatile per-turn noise near the front of the prompt
- do not make outbound Alfred messages recursively trigger new analysis

## 8. Teams and Graph specifics

### 8.1 Media

The Teams media session is configured to request unmixed meeting audio so the
system can use richer speaker information when available.

Relevant code:

- `src/Services/TeamsCallingBotService.cs`
- `src/Services/CallHandler.cs`

`CallHandler` also logs active-speaker and dominant-speaker telemetry from the
Teams media socket.

### 8.2 Graph chat ingress

Relevant code:

- `src/Services/MeetingChatService.cs`
- `src/Controllers/GraphNotificationController.cs`
- `src/Services/GraphNotificationProcessor.cs`

The service supports:

- tracking active meeting chat thread ids
- Graph subscription creation
- Graph subscription renewal
- lifecycle event handling
- resource-data decryption when configured
- fallback GET fetches when resource data is not present

### 8.3 Outbound chat send

Relevant code:

- `src/Controllers/SendChatController.cs`
- `src/Services/ConversationReferenceStore.cs`

Important protections already exist:

- per-thread send serialization
- duplicate send suppression

## 9. Product spec

The product spec lives at:

- `python/legionmeet_platform/specs/alfred.yaml`

This spec defines product-level intent and route behavior. It should stay
aligned with the runtime implementation.

If the code and spec disagree, fix the disagreement instead of letting both
drift.

## 10. Config surface

Primary example config:

- `src/Config/appsettings.example.json`

Important `MeetingChat` config values:

- `Enabled`
- `GraphNotificationBaseUrl`
- `GraphSubscriptionEncryptionCertPath`
- `GraphSubscriptionEncryptionCertPassword`
- `GraphSubscriptionEncryptionCertId`
- `ChatSubscriptionClientStateSecret`
- `ChatSendMaxRps`
- `TeamsAppCatalogId`
- `UseInstalledToChatsSubscription`

Important sink config and env:

- `PRODUCT_SPEC_PATH`
- `VARIANT_ID`
- `INSTANCE_ID`
- `SINK_HOST`
- `SINK_PORT`
- `SINK_URL`

## 11. Local runbook

### 11.1 Python sink

```bash
cd /home/azureuser/workspace/projects/teams-bot-poc/python
PRODUCT_SPEC_PATH=legionmeet_platform/specs/alfred.yaml \
VARIANT_ID=alfred \
INSTANCE_ID=alfred \
SINK_HOST=127.0.0.1 \
SINK_PORT=8765 \
.venv/bin/python transcript_sink.py
```

### 11.2 Streamlit UI

```bash
cd /home/azureuser/workspace/projects/teams-bot-poc/python
PRODUCT_SPEC_PATH=legionmeet_platform/specs/alfred.yaml \
VARIANT_ID=alfred \
INSTANCE_ID=alfred \
SINK_URL=http://127.0.0.1:8765 \
.venv/bin/streamlit run streamlit_ui.py \
  --server.port 8501 --server.address 127.0.0.1 --server.headless true
```

### 11.3 C# bot

`dotnet` is available at:

```bash
/home/azureuser/.dotnet/dotnet
```

Build:

```bash
cd /home/azureuser/workspace/projects/teams-bot-poc
/home/azureuser/.dotnet/dotnet build
```

If you want `dotnet` on `PATH` in your shell:

```bash
export PATH=/home/azureuser/.dotnet:$PATH
```

## 12. Tests and verification

Python:

```bash
cd /home/azureuser/workspace/projects/teams-bot-poc/python
uv run pytest
```

C#:

```bash
cd /home/azureuser/workspace/projects/teams-bot-poc
/home/azureuser/.dotnet/dotnet build
```

As of the current branch state:

- Python tests pass: `78 passed, 2 skipped`
- C# build passes: `0 warnings, 0 errors`

## 13. Editing rules for future LLMs

When modifying this repo, keep these principles intact:

1. Preserve the single canonical meeting ledger.
2. Preserve one authoritative inbound meeting chat path.
3. Preserve proactive Bot Framework send for Alfred outbound chat.
4. Keep AlfredAction as the live agent contract.
5. Prefer additive state updates over ad hoc prompt reconstruction.
6. Treat `ALFRED.md` and `alfred.yaml` as system documents, not afterthoughts.

## 14. What is still external or tenant-dependent

The code is implemented and locally verified, but some behaviors still depend on
real tenant configuration:

- Teams app installation scope
- Graph permissions and consent
- Graph notification reachability from the public internet
- encryption certificate deployment
- production-safe persistence and operational policy choices

Those are deployment concerns, not missing local implementation.
