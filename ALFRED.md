# ALFRED.md — system map for LLM operators

> **Purpose of this file.** You are an LLM landing in this repo. Read this
> first. It tells you exactly what Alfred is, which parts are real, which
> parts are scaffolded, and how to run / test / extend the system without
> re-deriving from the code. Everything here is verified against the
> current `feat/alfred-chat-modality` branch.

---

## 1. What Alfred is

Alfred is a **passive Microsoft Teams meeting assistant**. It joins a Teams
meeting through a Graph Communications / compliance-recording bot, captures
three streams of signal, and emits one of three per-turn actions:

| Input stream | Source |
|---|---|
| Live diarized speech (transcript) | C# bot media socket → Deepgram/Azure Speech → sink |
| Meeting chat messages (read) | Teams → Bot Framework adapter (live today) + Graph change-notification subscription (scaffolded) → sink |
| Meeting chat messages (send) | Sink → C# bot Bot Framework `ContinueConversationAsync` → Teams |

| Output action | Meaning |
|---|---|
| `SILENT` | Alfred's default. Update running notes / summary / topics. Do not post. |
| `SEND`   | Post a short, terse chat message into the meeting chat. |
| `ASK`    | Post a single clarifying question into the meeting chat. |

Alfred's persona and prompt are driven by a **product spec** (YAML), not
hardcoded. See `python/legionmeet_platform/specs/alfred.yaml`.

The previous identity of this repo — an "interview coach" — has been
deleted on `feat/alfred-chat-modality`. `main` still has it if you need
it back: `git checkout main -- <path>`.

---

## 2. Architecture at a glance

```
                      ┌─────────────────────────────────────────────────┐
 Teams meeting        │                   C# bot (src/)                 │
    ├── audio ───────►│  TeamsCallingBotService + CallHandler           │──┐
    │                 │  Deepgram/Azure Speech realtime transcriber     │  │ POST /transcript
    ├── chat ────────►│  AlfredBot (TeamsActivityHandler)               │──┤
    │                 │  MeetingChatService (Graph subscriptions TODO)  │  │ POST /chat
    │                 │  SendChatController /api/send-chat              │  │
    │                 │  MessagesController /api/messages (Bot FW)      │  │
    │                 │  GraphNotificationController /api/graph-notif.  │  │
    │                 │  CallingController /api/calling                 │  │
    │                 └─────────────────────────────────────────────────┘  │
    │                                        ▲                             │
    │                                        │ POST /api/send-chat         │
    │                                        │ (SEND/ASK intents)          │
    │                                        │                             ▼
    │                 ┌────────────────────────────────────────────────────┐
    │                 │           Python sink (python/, FastAPI)           │
    │                 │  transcript_sink.py                                │
    │                 │    POST /transcript   POST /chat                   │
    │                 │    GET  /session/status  (unified timeline)        │
    │                 │  meeting_agent/  (session + analyzer + models)     │
    │                 │  variants/alfred.py  (SILENT/SEND/ASK)             │
    │                 │  legionmeet_platform/routes/teams_chat.py          │
    │                 └────────────────────────────────────────────────────┘
    │                                        │
    │                                        │ HTTP polling
    │                                        ▼
    │                 ┌────────────────────────────────────────────────────┐
    ◄── chat ─────────│         Streamlit UI (python/streamlit_ui.py)      │
                      │   Left: unified timeline  |  Right: notebook       │
                      │   Compose box sends into the sink via POST /chat   │
                      └────────────────────────────────────────────────────┘
```

Three processes:

1. **C# bot** (`src/TeamsMediaBot.csproj`, net8.0) — joins Teams calls, owns
   the media socket + chat I/O. Runs as a Windows service in prod.
2. **Python sink** (`python/transcript_sink.py`, FastAPI) — owns session
   state, the unified timeline, the analyzer, and the routing layer.
3. **Streamlit UI** (`python/streamlit_ui.py`) — human observer UI; polls
   the sink, lets a user compose and send chat as themselves or as Alfred.

---

## 3. Repo layout (what to read first)

```
teams-bot-poc/
├── ALFRED.md                           ← you are here
├── CLAUDE.md                           ← VM / OpenClaw system map (ignore for Alfred)
├── manifest/manifest.json              ← Teams app manifest (schema v1.21)
├── scripts/
│   ├── install-bot-in-chat.ps1         ← one-time per-meeting RSC install
│   ├── setup-policy-auto-invite.ps1    ← compliance recording policy setup
│   ├── join_meeting.sh                 ← operator wrapper to trigger a join
│   └── vm-*.ps1, bootstrap-production-vm.ps1
│
├── src/                                ← C# bot
│   ├── Program.cs                      ← Kestrel + DI wiring
│   ├── TeamsMediaBot.csproj            ← packages (Bot Builder 4.22 + Graph 5.92)
│   ├── Config/appsettings.example.json ← Bot / MediaPlatformSettings / MeetingChat
│   ├── Models/BotConfiguration.cs      ← config classes
│   ├── Controllers/
│   │   ├── CallingController.cs        ← /api/calling (Graph Communications)
│   │   ├── MessagesController.cs       ← /api/messages (Bot Framework)
│   │   ├── SendChatController.cs       ← /api/send-chat (internal)
│   │   └── GraphNotificationController.cs ← /api/graph-notifications
│   └── Services/
│       ├── TeamsCallingBotService.cs   ← Graph Communications SDK bot
│       ├── CallHandler.cs              ← per-call lifecycle + audio socket
│       ├── AlfredBot.cs                ← Bot Framework activity handler
│       ├── ConversationReferenceStore.cs ← in-memory conv-ref cache
│       ├── PythonChatPublisher.cs      ← POSTs to sink /chat
│       ├── PythonTranscriptPublisher.cs ← POSTs to sink /transcript
│       ├── MeetingChatService.cs       ← Graph subscription lifecycle (TODO)
│       └── Deepgram/AzureSpeech*.cs    ← STT providers
│
└── python/
    ├── requirements.txt                ← fastapi, pydantic, pyyaml
    ├── transcript_sink.py              ← FastAPI app
    ├── streamlit_ui.py                 ← the note-taking UI
    ├── run_variant_sink.py             ← CLI wrapper to launch one sink instance
    ├── run_variant_ui.py               ← CLI wrapper to launch one UI instance
    ├── meeting_agent/                  ← (was interview_agent/)
    │   ├── agent.py                    ← InterviewAnalyzer (still emits interview output;
    │   │                                  AlfredActionOutput reshape is TODO)
    │   ├── models.py                   ← TranscriptEvent, ChatMessage, AlfredAction,
    │   │                                  AnalysisItem, InterviewSession, SessionAnalysis
    │   ├── session.py                  ← InterviewSessionManager + get_unified_timeline
    │   ├── checklist.py / checklist_state.py ← topic-tracking checklist
    │   ├── output.py, pubsub.py        ← analysis persistence + live thought stream
    │   └── __init__.py
    ├── variants/
    │   ├── base.py                     ← VariantPlugin protocol + on_chat_message hook
    │   ├── alfred.py                   ← the only registered variant
    │   ├── registry.py                 ← maps "alfred" → AlfredVariantPlugin
    │   └── __init__.py
    ├── legionmeet_platform/
    │   ├── spec_loader.py              ← loads JSON *or* YAML specs
    │   ├── spec_models.py              ← ProductSpec, UiSpec, OutputRouteSpec, etc.
    │   ├── specs/alfred.yaml           ← *the* Alfred product spec
    │   └── routes/
    │       ├── router.py               ← build_route_orchestrator(spec)
    │       ├── ui_stream.py            ← in-process fanout for the UI
    │       ├── webhook.py              ← generic HTTP POST route
    │       └── teams_chat.py           ← SEND/ASK → bot /api/send-chat
    └── tests/
        ├── test_sink.py                ← FastAPI tests incl. /chat and timeline
        ├── test_variants.py            ← alfred variant tests
        ├── test_product_spec.py        ← spec loader + YAML + teams_chat validation
        └── mock_data.py                ← generators for transcript events
```

---

## 4. What's real vs. what's scaffolded

Before editing anything, know this — it saves you from thinking a working
piece is broken or a TODO stub is ready.

### Real + tested

- Python sink end-to-end: `POST /transcript`, `POST /chat`, `/session/start`,
  `/session/end`, `/session/status`, `/session/analysis`. **77 pytest passing.**
- Unified timeline (speech + chat merged by timestamp) in
  `session.py:get_unified_timeline` and exposed via `/session/status.session.meeting_history`.
- `teams_chat` route: fires only on `alfred_action.action in {SEND, ASK}`,
  rate-limited client-side. Posts to the C# bot's `/api/send-chat`.
- Alfred variant: registered as the only variant. Adds `trigger_kind`,
  `action_menu`, `bias_toward_silence` to the analysis context.
- YAML spec loading (`spec_loader._parse_spec_file`).
- C# bot compiles clean (`dotnet build` → 0/0) with Bot Framework 4.22 wired.
- `AlfredBot` (Bot Framework `TeamsActivityHandler`): captures
  `ConversationReference` on conversationUpdate / message activities and
  forwards inbound chat to the Python sink's `/chat`.
- `SendChatController` `/api/send-chat`: resolves a `ConversationReference`
  and posts via `CloudAdapter.ContinueConversationAsync`. Server-side semaphore.
- Streamlit UI: imports clean, boots on Streamlit, serves 200.
  Unified timeline + notebook + compose box.
- Manifest v1.21 with 7 RSC application permissions.

### Scaffolded (compiles, does not yet do the work)

Each of these is intentionally marked in-code with `TODO[Alfred]`:

- `src/Services/MeetingChatService.cs` — `AttachToCallAsync` does not yet
  resolve `chatInfo.threadId` from the call, create the Graph subscription,
  or renew it. It's a correct interface + lifecycle skeleton.
- `src/Controllers/GraphNotificationController.cs` — handles the Graph
  validation-token handshake for real; does not yet decrypt the encrypted
  resource-data payload or fan out to `PythonChatPublisher`.
- `src/Services/TeamsCallingBotService.cs` + `CallHandler.cs` — do not yet
  call `IMeetingChatService.AttachToCallAsync(call)` on call-established /
  detach on call-removed.
- `python/meeting_agent/agent.py` — still uses `InterviewAnalysisOutput`.
  The envelope `AnalysisItem.alfred_action` is ready, but the analyzer
  itself is not yet reshaped to emit `AlfredAction` directly. Until it is,
  Alfred's notes/summary/topics come from whatever the interview analyzer
  happens to populate in `key_points`, with graceful fallbacks in the UI
  (`_collect_alfred_notes`).

### Deferred by design

- Durable `ConversationReferenceStore` (Redis/SQLite) — process-local is
  fine for the POC.
- `streamlit_alfred.py` as a second UI — we rewrote `streamlit_ui.py` in
  place instead. The interview UI is recoverable from `main`.

---

## 5. Running it locally

### 5.1 Prereqs on this VM

- `.venv/bin/python` at `python/.venv/bin/python` (already set up)
- `dotnet` at `/home/azureuser/.dotnet/dotnet` (NOT in `$PATH`). Either
  export it or use the full path:
  ```bash
  export PATH=/home/azureuser/.dotnet:$PATH
  ```

### 5.2 Python sink

```bash
cd /home/azureuser/workspace/projects/teams-bot-poc/python
PRODUCT_SPEC_PATH=legionmeet_platform/specs/alfred.yaml \
VARIANT_ID=alfred \
INSTANCE_ID=alfred \
SINK_HOST=127.0.0.1 \
SINK_PORT=8765 \
.venv/bin/python transcript_sink.py
```

Required env vars (the sink fails fast otherwise):
- `PRODUCT_SPEC_PATH` — path to an Alfred-compatible spec.
- `VARIANT_ID` — `alfred` is the only registered variant.
- `INSTANCE_ID` — freeform, used in output paths and telemetry.
- `SINK_HOST`, `SINK_PORT`.

Health check: `curl http://127.0.0.1:8765/health`.

### 5.3 Streamlit UI

```bash
cd /home/azureuser/workspace/projects/teams-bot-poc/python
PRODUCT_SPEC_PATH=legionmeet_platform/specs/alfred.yaml \
VARIANT_ID=alfred \
INSTANCE_ID=alfred \
SINK_URL=http://127.0.0.1:8765 \
.venv/bin/streamlit run streamlit_ui.py \
  --server.port 8501 --server.address 127.0.0.1 --server.headless true
```

Open `http://127.0.0.1:8501`. The `SINK_URL` env var tells the UI where
the sink lives.

### 5.4 C# bot

```bash
cd /home/azureuser/workspace/projects/teams-bot-poc/src
/home/azureuser/.dotnet/dotnet build
/home/azureuser/.dotnet/dotnet run \
  --project TeamsMediaBot.csproj \
  -- --config Config/appsettings.json
```

`Config/appsettings.json` is gitignored and must be derived from
`Config/appsettings.example.json`. Required sections: `Bot`,
`MediaPlatformSettings`, `Stt`, `TranscriptSink`, `MeetingChat`, plus
Bot Framework keys `MicrosoftAppId` / `MicrosoftAppPassword` /
`MicrosoftAppTenantId` / `MicrosoftAppType=SingleTenant`.

### 5.5 Full smoke test (no meeting required)

With the sink running:

```bash
# Start a session
curl -s -X POST http://127.0.0.1:8765/session/start \
  -H 'Content-Type: application/json' \
  -d '{"candidate_name":"Demo","meeting_url":"https://teams.microsoft.com/meet/x"}'

# Post a chat message
curl -s -X POST http://127.0.0.1:8765/chat \
  -H 'Content-Type: application/json' \
  -d '{"chat_thread_id":"19:m@thread.v2","message_id":"m1",
       "text":"Hello Alfred","sender_display_name":"Alice","sender_id":"a",
       "timestamp_utc":"2026-04-22T16:00:00Z",
       "conversation_reference_id":"ref-xyz"}'

# Post a transcript turn
curl -s -X POST http://127.0.0.1:8765/transcript \
  -H 'Content-Type: application/json' \
  -d '{"event_type":"final","text":"Bob says hi","speaker_id":"speaker_0",
       "timestamp_utc":"2026-04-22T16:00:30Z"}'

# Inspect the unified timeline
curl -s http://127.0.0.1:8765/session/status | jq '.session.meeting_history'
```

You should see the chat then the speech, in timestamp order.

---

## 6. Key HTTP contracts

### 6.1 Sink (Python, FastAPI)

- `POST /transcript` — body: `TranscriptEventRequest` (v1 *or* v2 format).
- `POST /chat` — body: `ChatMessageRequest`:
  ```json
  {
    "event_type": "chat_created|chat_updated|chat_deleted",
    "chat_thread_id": "19:meeting_*@thread.v2",
    "message_id": "...",
    "text": "...",
    "html": null,
    "sender_id": "...",
    "sender_display_name": "...",
    "timestamp_utc": "ISO8601 with Z",
    "conversation_reference_id": "...",
    "attachments": [], "mentions": [],
    "reply_to_message_id": null,
    "from_bot": false,
    "raw": null
  }
  ```
- `POST /session/start` — body: `{candidate_name, meeting_url, product_id?, candidate_speaker_id?}`.
- `POST /session/end` — no body.
- `POST /session/map-speaker` — body: `{speaker_id, role}`. Valid roles:
  `candidate`, `interviewer`, `participant`, `bot`.
- `GET /session/status` — returns a `SessionStatusWrapper` containing:
  - `session.meeting_history` — **the unified timeline** (use this).
  - `session.recent_conversation` — legacy speech-only view.
  - `session.chat_messages_count`, `session.conversation_reference_id`.
  - `session.checklist` — current Alfred progress items.
- `GET /session/analysis` — `SessionAnalysis`. Includes
  `running_summary` and `topics` (populated once the agent is reshaped).
- `GET /product/spec` — introspects the active product spec.
- `GET /stats`, `GET /health`.

### 6.2 Bot (C#, ASP.NET Core)

- `POST /api/calling` — Graph Communications calling webhook. Owned by
  `CallingController`.
- `POST /api/messages` — Bot Framework messaging endpoint. Routed to
  `AlfredBot` via `CloudAdapter`. Captures `ConversationReference`;
  forwards inbound chat to sink `/chat`.
- `POST /api/send-chat` — internal endpoint the sink's `teams_chat` route
  calls when Alfred emits SEND/ASK. Body: `SendChatRequest`:
  ```json
  {
    "conversation_reference_id": "<chat thread id>",
    "action": "SEND|ASK",
    "text": "...",
    "mentions": [],
    "reply_to_message_id": null
  }
  ```
- `POST /api/graph-notifications` — Graph change-notification receiver.
  Validation handshake live; payload decrypt TODO.
- `GET /health` — aspnet health check.

---

## 7. Data shapes you'll touch

- `meeting_agent/models.py`:
  - `TranscriptEvent` — STT input, v2.
  - `ChatMessage` — meeting chat, canonical.
  - `AlfredAction` — `action`/`rationale`/`chat_text`/`mentions`/`reply_to_message_id`/
    `notes`/`running_summary`/`topics`.
  - `AnalysisItem` — envelope; scores optional; `alfred_action` optional.
  - `InterviewSession` (**generic now**; name kept for churn reasons) —
    contains `transcript_events`, `chat_messages`, `conversation_reference_id`.
  - `SessionAnalysis` — persisted analysis, now carries
    `running_summary` + `topics`.

- `variants/base.py` protocol methods:
  - `on_session_start`, `on_transcript`, `on_chat_message` (new),
    `on_session_end`, `build_analysis_context`, `transform_analysis_item`.

- `legionmeet_platform/spec_models.py`:
  - `OutputRouteType` — `ui_stream`, `webhook`, `teams_chat`, `teams_dm`
    (the last still unimplemented).
  - `OutputRouteSpec.max_rps` — client-side throttle (default 4).

---

## 8. Tests

```bash
cd /home/azureuser/workspace/projects/teams-bot-poc/python
.venv/bin/python -m pytest tests/ -q
```

Current: **77 passed, 2 skipped.** `test_sink.py::TestChatEndpoint`
covers the `/chat` endpoint, timeline ordering, and deleted-message drop.

```bash
cd /home/azureuser/workspace/projects/teams-bot-poc/src
/home/azureuser/.dotnet/dotnet build
```

Current: **0 warnings, 0 errors.**

---

## 9. Common LLM tasks

### 9.1 "Add a new Alfred hook / change the action menu"

1. Edit the prompt in `python/legionmeet_platform/specs/alfred.yaml`
   under `agent.prompt_template`.
2. The variant (`variants/alfred.py`) sets `action_menu` and
   `bias_toward_silence` on the context. Adjust there if you want new
   action keywords.
3. `AlfredAction` in `meeting_agent/models.py` currently constrains
   `action` to `SILENT | SEND | ASK`. Widening requires a `Literal[...]`
   change and tests in `tests/test_variants.py`.

### 9.2 "Wire the Graph chat subscription"

Touchpoints, in order:

1. `src/Services/MeetingChatService.cs` — finish `AttachToCallAsync`:
   extract `JoinUrl` from `ICall.Resource`, call
   `GET /users/{upn}/onlineMeetings/getByJoinWebUrl(...)` to resolve
   `chatInfo.threadId`, then `POST /subscriptions`.
2. `src/Controllers/GraphNotificationController.cs` — implement the
   decrypt path and fan out to `PythonChatPublisher`.
3. `src/Services/CallHandler.cs` / `TeamsCallingBotService.cs` — call
   `_meetingChatService.AttachToCallAsync(call)` on call-established and
   `DetachFromCallAsync` on call-removed.
4. `MeetingChatConfiguration` already has the config knobs:
   `GraphNotificationBaseUrl`, `GraphSubscriptionEncryptionCertPath`,
   `ChatSubscriptionClientStateSecret`.

2026 docs to cite in your commit message:

- <https://learn.microsoft.com/en-us/graph/teams-changenotifications-chatmessage>
- <https://learn.microsoft.com/en-us/graph/change-notifications-with-resource-data>
- <https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/rsc/resource-specific-consent>

### 9.3 "Make Alfred actually emit SILENT/SEND/ASK"

Reshape `meeting_agent/agent.py::InterviewAnalyzer`:

1. Set `output_type=AlfredAction` on the `Agent(...)` call.
2. Replace `_build_prompt` to frame the task around the unified timeline
   (`context["meeting_history"]`) + action menu + mute flag.
3. Have the analyzer stuff its result into
   `AnalysisItem.alfred_action` instead of `relevance_score`/`clarity_score`.
4. Drop the candidate-only gating in
   `python/transcript_sink.py::queue_latest_candidate_turn` — Alfred
   analyses every final turn and every chat message. Rename the
   function if you feel like it; the envelope is generic already.

The `teams_chat` route already pulls from `alfred_action.action` and
ignores anything else.

### 9.4 "Install Alfred into a real meeting chat"

Policy-based compliance recording joins ≠ RSC chat install. Two steps
required per meeting (one-time per tenant for step A):

A. Admin consents to the manifest. Manifest has 7 RSC application perms
   already (`ChatMessage.Read.Chat`, etc.).
B. Run `scripts/install-bot-in-chat.ps1` once per meeting chat, passing
   the organizer UPN and meeting join URL. Requires the
   `TeamsAppInstallation.ReadWriteForChat.All` application permission
   with admin consent on the bot's Entra app.

Alternative (broader consent, no per-meeting install): swap RSC
`ChatMessage.Read.Chat` subscriptions for tenant-wide `Chat.Read.All`
on `/chats/getAllMessages`, then filter by `chat_thread_id` in the C#
handler.

### 9.5 "Run two Alfreds side by side"

- Copy `alfred.yaml` to e.g. `alfred-engineering.yaml`, change
  `product_id`, tweak the prompt.
- `python run_variant_sink.py --instance engineering --port 8766 \
  --product-spec legionmeet_platform/specs/alfred-engineering.yaml`
- `python run_variant_ui.py --instance engineering --port 8502 \
  --sink-url http://127.0.0.1:8766 \
  --product-spec legionmeet_platform/specs/alfred-engineering.yaml`
- Each sink gets its own `INSTANCE_ID`, output dir, transcript file, and
  checklist state. The C# bot can POST the same `chat_thread_id` to
  multiple sinks by configuring `TranscriptSink.ChatEndpoint` per
  instance (out of scope: that'd need a fan-out service).

### 9.6 "Debug: Alfred never posts chat even though action=SEND"

Check, in order:

1. `GET /session/status` → `session.conversation_reference_id` must be
   non-null. Null means the bot has never seen an inbound chat activity
   in that thread, so no `ConversationReference` was captured.
2. The `teams_chat` route in the active product spec must be
   `enabled: true` with a non-null `url` pointing at the bot's
   `/api/send-chat`. The YAML ships with `enabled: false` by default.
3. The bot's `/api/send-chat` responds 404 when there's no stored
   reference — look for `No ConversationReference for that chat` in the
   bot logs.
4. Mute toggle: the UI mute only gates the "Send as Alfred" button,
   but `alfred_action.action="SEND"` payloads still flow. To mute
   server-side you need to filter in `TeamsChatRoute.dispatch`.

---

## 10. Guardrails (what NOT to do)

- **Do not** re-introduce the interview variant on this branch. `main`
  is the escape hatch if anyone misses it. See the memory file
  `feedback_branch_and_overwrite_style.md`.
- **Do not** try to `POST /chats/{id}/messages` with application
  permissions. In 2026 the only documented app permission for that
  endpoint is `Teamwork.Migrate.All`, which Microsoft explicitly says is
  not for live bot messaging. Use the Bot Framework proactive path —
  that's why `SendChatController` exists.
- **Do not** enable a chat subscription on a tenant without a real cert
  path wired. `MeetingChatConfiguration.GraphSubscriptionEncryptionCertPath`
  must be set (PFX) before `includeResourceData=true` subscriptions can
  be created.
- **Do not** remove `InterviewSession` / `InterviewSessionManager`
  class names casually. They're generic now but touched by many call
  sites and the output file naming convention (`int_YYYYMMDD_...`). A
  rename is fine but needs to be audited across `meeting_agent/`,
  `transcript_sink.py`, and `tests/`.
- **Do not** assume `dotnet` is on `$PATH`. It lives at
  `/home/azureuser/.dotnet/dotnet` on this VM.

---

## 11. Source of truth for 2026 Teams capabilities

All Teams/Graph claims in this repo are grounded in official Microsoft
Learn documentation. When extending, cite:

- Bots for Teams calls and online meetings:
  <https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/calls-meetings-bots-overview>
- Teams change notifications (chatMessage):
  <https://learn.microsoft.com/en-us/graph/teams-changenotifications-chatmessage>
- Resource-specific consent:
  <https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/rsc/resource-specific-consent>
- Proactive messages:
  <https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages>
- Compliance recording:
  <https://learn.microsoft.com/en-us/microsoftteams/teams-recording-compliance>

The full plan that produced this branch is at
`~/.claude/plans/research-2026-documentation-for-ethereal-octopus.md`.

---

## 12. Quick reference — the 30-second version

```bash
# Verify everything still builds and tests pass
cd python && .venv/bin/python -m pytest tests/ -q                  # 77 passed
cd ../src && /home/azureuser/.dotnet/dotnet build                   # 0/0

# Run the stack locally (3 terminals)
#   T1: sink
cd python && PRODUCT_SPEC_PATH=legionmeet_platform/specs/alfred.yaml \
  VARIANT_ID=alfred INSTANCE_ID=alfred SINK_HOST=127.0.0.1 SINK_PORT=8765 \
  .venv/bin/python transcript_sink.py
#   T2: UI
cd python && SINK_URL=http://127.0.0.1:8765 \
  PRODUCT_SPEC_PATH=legionmeet_platform/specs/alfred.yaml \
  VARIANT_ID=alfred INSTANCE_ID=alfred \
  .venv/bin/streamlit run streamlit_ui.py --server.port 8501 --server.headless true
#   T3: bot (needs appsettings.json configured per your tenant)
cd src && /home/azureuser/.dotnet/dotnet run -- --config Config/appsettings.json
```

If something's broken, start from `GET /health` on the sink and
`dotnet build` on the bot. Most misconfiguration surfaces there.
