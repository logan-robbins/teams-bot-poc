# Alfred

Microsoft Teams meeting assistant for The Walt Disney Company. Joins a
Teams meeting, captures diarized audio + chat, runs an LLM agent that
maintains a live dossier (decisions, open questions, action items,
risks) keyed per-meeting, and posts back into the meeting chat under
explicit intervention rules. Also persistently attaches to a Teams
**channel** to listen to all posts and roll up every meeting that
happens in it (§5b).

> **AI coding agents:** read [`AGENTS.md`](AGENTS.md) first — it has
> the operational layer (build, deploy, debug, gotchas) underneath
> this product overview.

This README is the **what + why**. `AGENTS.md` is the **how**.
`PROD.md` tracks productionalization status and deferred work.

## TL;DR

- **The bot is a Teams platform.** It captures audio + chat for every
  channel it's attached to, and POSTs each event as a versioned
  envelope ([`alfred-events-v1`](docs/event-contract.md)) to every
  consumer URL registered against that channel. Each team owns
  whatever's behind their URL.
- **Three deployables in this repo:** C# bot on a Windows VM (audio +
  Teams APIs + per-channel consumer registry + outbound dispatcher),
  Python FastAPI sink on Container Apps (the reference consumer +
  sessions + agent + SQLite), React UI on Container Apps (read-only,
  reads the reference sink).
- **Single key:** `chat_thread_id` routes every transcript and chat
  to its session. The UI URL is `/m/<chat_thread_id>`.
- **Channel rollup:** every event is also stamped with optional
  `team_id` / `channel_id` / `channel_thread_id`, so analytics can
  query an entire channel's history (chat + every meeting +
  every transcript) by `channel_id` alone.
- **Agent contract:** one `AlfredExtraction` per debounced tick;
  optionally one tool call (`send_to_meeting_chat`). No `SEND/ASK/SILENT`
  enum. Silence is "did not call the tool".
- **Source of truth for agent behavior:** `python/batcave_platform/specs/alfred.yaml`.

---

## 1. System

```
       Microsoft Teams (meeting OR persistent channel)
                        │
       audio PCM        │  Bot Framework chat activities (/api/messages)
       + roster         │  Graph change-notifications (/api/graph-notifications)
                        ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  C# bot  src/  ·  Windows VM  ·  Graph Communications + Bot SDK    │
  │                                                                    │
  │  Audio path:  Join call → stream PCM → diarized TranscriptEvents   │
  │               (Azure Speech, the only sanctioned Disney provider)  │
  │  Chat path:   Bot Framework + Graph subscriptions                  │
  │  Channel:     Persistent attach → ChannelMessage subscription      │
  │               + auto-attach on team-install (§5b)                  │
  │  Identity:    MSI ↔ AAD ↔ display_name from ICall.Participants     │
  │  Audit:       Per-meeting NDJSON log on disk (§4)                  │
  │  Outbound:    send_to_meeting_chat via Bot Framework adapter       │
  │                                                                    │
  │  Stamps every published event with chat_thread_id (always) and     │
  │  team_id / channel_id / channel_thread_id (when known) so the      │
  │  sink can later roll meetings up under their parent channel.       │
  └──────────────────┬─────────────────────────────────────────────────┘
                     │
                     │  POST /transcript    POST /chat    POST /session/link
                     │  POST /session/participants     POST /api/send-chat ◀── reverse
                     ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  Python sink  python/  ·  FastAPI on Container Apps                │
  │                                                                    │
  │  E1 raw layer  (immutable):                                        │
  │      raw_ingest_events — every inbound event hashed, BEFORE any    │
  │        filter (partial drop, session-active, echo suppression)     │
  │                                                                    │
  │  E2 working layer  (cleanable):                                    │
  │      SessionRegistry — one InterviewSessionManager per             │
  │        chat_thread_id; auto-starts on first non-empty final/chat   │
  │      meeting_events — normalized ledger; back-links to raw via     │
  │        source_raw_event_ids; team_id/channel_id stamped at write   │
  │        time and backfilled by /session/link                        │
  │                                                                    │
  │  E3 identity:  ParticipantResolver  manual > teams_msi_unique >    │
  │                teams_msi_group > sole_human > unresolved           │
  │                                                                    │
  │  Agent:    AlfredAnalyzer — debounced, one AlfredExtraction per    │
  │            tick (rolling summary + topics + notes + decisions +    │
  │            open_questions + action_items + risks, merged by id)    │
  │  E4 policy:  alfred.yaml interventions + 45 s cooldown +           │
  │              directly-addressed bypass                             │
  │  Outbound:  send_to_meeting_chat (sole tool)                       │
  │                                                                    │
  │  Channel rollup:                                                   │
  │      session_channel_links binds chat_thread_id → channel context  │
  │      /c/{teamId}/{channelId}/events returns chat + every meeting   │
  │        + every transcript under one channel, ordered by ts         │
  │                                                                    │
  │  SSE /m/{chat_thread_id}/events drives the UI                      │
  └──────────────────┬─────────────────────────────────────────────────┘
                     │  SSE + JSON, filtered by chat_thread_id
                     ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  React UI  web/  ·  Vite + React 19                                │
  │     /           → MeetingList (polls /m every 2 s)                 │
  │     /m/<id>     → MeetingDossier (Ledger / Dossier / Companion).   │
  │                   Read-only — only Alfred speaks via the tool.     │
  └────────────────────────────────────────────────────────────────────┘
```

### Key contracts

**Meeting key.** `chat_thread_id` is the canonical id —
`19:meeting_xxx@thread.v2` for a meeting, `19:{channelId}@thread.tacv2`
for a channel. Every transcript and chat carries it. The UI URL
*requires* it (`/m/<chat_thread_id>`); knowing the id IS the access
boundary, there is no other auth on the per-meeting routes.

**Channel rollup key.** For a channel meeting, `channel_thread_id` is
the *parent channel's* id, distinct from this meeting's
`chat_thread_id`. Stamped on events when the bot sees `channelData`
on a meeting-chat activity, plus retroactively backfilled via
`POST /session/link`. Once linked, `GET /c/{teamId}/{channelId}/events`
returns every event under one ordered timeline.

**Agent contract.** One `AlfredExtraction` per tick, optionally one
`send_to_meeting_chat` tool call. Do not introduce a `SEND/ASK/SILENT`
enum. Do not introduce a parallel outbound path.

**Debouncing contract.** C# does not debounce Alfred; it forwards
*every* STT event to `/transcript` and *every* chat to `/chat`. Python
owns the ledger and the agent's batching:
`python/meeting_agent/debounce.py` (`DEFAULT_QUIET_WINDOW_SECONDS=1.5`,
`DEFAULT_MAX_BATCH=8`) used by `python/transcript_sink.py`.
`partial` transcripts are raw-audited but not promoted to the working
ledger; `final` transcripts and chat messages can trigger Alfred.

---

## 2. Disney environment

| Thing | Value |
|---|---|
| Azure subscription | `e02c0038-82c8-4655-9647-38083f301099` (WDI R&D) |
| M365 tenant (Teams + Entra app) | `38387f0b-9a6f-46e2-8373-67422f8c2cb0` (plutosdoghouse.com) |
| Resource group | `rg-alfred-disney` |
| Bot VM | `vm-alfred-disney` (`alfred-disney-bot.eastus.cloudapp.azure.com`) |
| Azure Bot Service | `bot-alfred-disney` (SingleTenant) |
| Bot AppId | `207a38a4-67c5-4ef9-ada8-ea7998734d59` |
| Sink (Container App) | `https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io` |
| UI (Container App) | `https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io` |
| ACR | `acralfreddisneye02c0038.azurecr.io` |
| Azure OpenAI | `aoai-alfred-disney` (`gpt-5-mini`, GlobalStandard cap 10) |
| Speech Services | `speech-alfred-disney` (eastus, S0) |

**Source of truth:** GitHub `private/main` at
`git@github.com:logan-robbins/alfred-teams-bot.git`. The `disney` remote
(`gitlab.wdi.disney.com/Michael.Barron.-ND/teams_integration`) hosts the
active merge-request branch (`alfred-agent-updates`) for Disney review.

---

## 3. Permissions (RSC)

The manifest declares 11 Resource-Specific Consent permissions split
across the two operational shapes — meeting and channel. There are
zero tenant-wide Graph permissions.

**Chat-scoped (per-meeting / per-chat install):**

| Permission | Used for |
|---|---|
| `Calls.JoinGroupCalls.Chat` | Bot enters the call as a participant. |
| `Calls.AccessMedia.Chat` | Receive 16 kHz / 16-bit / mono PCM audio. |
| `OnlineMeetingParticipant.Read.Chat` | MSI ↔ AAD ↔ display-name lookup (E3). |
| `ChatMessage.Read.Chat` | Graph subscription on the meeting chat. |
| `ChatMessageReadReceipt.Read.Chat` | Reserved (planned engagement signal). |

**Team-scoped (persistent channel attachment, §5b):**

| Permission | Used for |
|---|---|
| `ChannelMessage.Read.Group` | Graph subscription on `teams/{teamId}/channels/{channelId}/messages`. |
| `ChannelMessage.Send.Group` | Optional Graph-based outbound (Bot Framework adapter is the default). |
| `ChannelMeeting.ReadBasic.Group` | Discover channel meetings spawned in attached channels. |
| `ChannelMeetingParticipant.Read.Group` | Roster lookups for channel meetings. |
| `TeamsAppInstallation.Read.Group` | Verify the bot is still installed at the team level. |
| `TeamSettings.Read.Group` | Read team display name / settings for operator UI. |

Plus two manifest-level Teams app permissions: `identity`,
`messageTeamMembers`. The manifest also requests
`supportsCalling: true`.

If the tenant treats Alfred as "unverified publisher", an M365 admin
must grant org-wide consent in **Teams Admin Center → Manage apps →
Permissions**. The manifest cannot bypass this.

---

## 4. Repo layout

| Folder | Role |
|---|---|
| `src/` | C# Teams bot. Graph Communications SDK + Bot Framework. Builds with `dotnet publish` (Windows-x64; verify locally with the Docker SDK image — see `AGENTS.md` §3.3). |
| `python/` | FastAPI sink + Alfred agent. Run with `uv run python run_variant_sink.py …` from this folder. |
| `python/meeting_agent/` | Canonical session/agent state. `models.py`, `session.py`, `persistence.py`, `agent.py`, `tools.py`, `identity.py`. |
| `python/batcave_platform/` | Product spec loader + output routes. `specs/alfred.yaml` is the **sole source of truth** for Alfred's prompt and intervention policy. |
| `python/tests/` | `uv run pytest tests` baseline: **121 passed, 2 skipped**. |
| `web/` | React 19 + Vite + Tailwind v4 UI. Per-meeting routing via `react-router-dom`. |
| `manifest/` | Teams app manifest (`manifest.json`, `alfred-sandbox.zip`). Bot AppId, 11 RSC permissions, valid domains. Currently at version `1.0.7`. |
| `scripts/` | Deploy + ops scripts. Canonical entrypoints: `deploy-azure-vm.sh`, `deploy-azure-agent.sh`, `bootstrap-production-vm.ps1`, `join_meeting.sh`. |
| `AGENTS.md` | **Operator manual for AI coding agents** — build, deploy, debug. Read this before changing anything in this tree. |

**Canonical state object:** `InterviewSession` in
`python/meeting_agent/models.py`. **Canonical history:**
`InterviewSession.meeting_events` (single append-only ledger of
normalized speech + chat + system events). One session per
`chat_thread_id`, held in `SessionRegistry`. The immutable raw layer
is `raw_ingest_events`; `MeetingEvent.source_raw_event_ids` back-links
every working-ledger row to the raw rows that produced it.

**Canonical channel link:** `session_channel_links` (chat_thread_id PK
→ team_id, channel_id, channel_thread_id). Populated either by C#
stamping at write time or by `POST /session/link`, which also
backfills prior `meeting_events` and `raw_ingest_events` rows.

---

## 5. Install in the M365 tenant

Alfred can be installed into a meeting chat (per-meeting), a group chat,
or a Teams **channel** (persistent — once attached, the bot listens to
every post in the channel and is allowed to post back). Pick whichever
matches the use case.

### 5a. Per-meeting install (existing flow)

1. Upload `manifest/alfred-sandbox.zip` via the Teams Developer Portal
   at `https://dev.teams.microsoft.com/apps` → **Import app**.
2. **Preview in Teams** → **Add to a chat**. (Do **not** use "Add to a
   meeting" — that flow opens a modal that renders `developer.websiteUrl`
   because the manifest is bot-only with no `configurableTabs`.)
3. From that chat, either invite the bot into a running meeting, or
   trigger an explicit join:
   ```bash
   ./scripts/join_meeting.sh "<teams-meeting-join-url>" "Alfred"
   ```
4. If the publisher is "unverified", see §3.

### 5b. Persistent channel attachment (sandbox-channel flow)

Channel attachment is the channel-level analog of "the bot is in this
meeting". Once attached the bot listens to every channel post via a
Graph change-notification subscription on
`teams/{teamId}/channels/{channelId}/messages` and is allowed to post
back via the existing `send_to_meeting_chat` tool. Attachments survive
bot restarts (state lives in `C:\teams-bot-poc\state\channel-attachments.json`
on the VM) and the renewal loop keeps the Graph subscription alive.

There are two paths in:

**Path A — install the app at the team level (preferred).** When Alfred
is added to a team via Teams Admin Center → Manage apps → *Install* into
a team, the bot receives a `membersAdded` event with the bot in the
member list. `AlfredBot.OnMembersAddedAsync` reads `TeamsChannelData`,
auto-attaches to the channel the install fired in, and creates the
Graph subscription.

**Path B — operator API attach by team-id + channel-id.** Useful when
you want to attach Alfred to a specific channel without re-installing.
Both ids come from Teams clients (`Get link to team/channel` →
`groupId=…&channelId=…`).

```bash
BOT=https://alfred-disney-bot.eastus.cloudapp.azure.com
TEAM_ID='<aad group id of the team>'
CHANNEL_ID='19:<channel guid>@thread.tacv2'

# Attach
curl -sS -X POST "$BOT/api/channels/attach" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg tid "$TEAM_ID" --arg cid "$CHANNEL_ID" \
        '{team_id:$tid, channel_id:$cid, source:"manual_attach"}')" | jq

# List active attachments (survives restarts)
curl -sS "$BOT/api/channels" | jq

# Detach (deletes the Graph subscription and removes the persistent record)
curl -sS -X DELETE "$BOT/api/channels/$TEAM_ID/$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=""))' "$CHANNEL_ID")"
```

After attach, every channel post is forwarded to the Python sink's
`/chat` endpoint with `conversation_kind:"channel"`, `team_id`, and
`channel_id` populated. The session is keyed on
`19:{channelId}@thread.tacv2` (the same id Bot Framework uses), so the
existing per-thread routes (`/m/<chat_thread_id>/...`) work unchanged.

Outbound: the bot posts back via `send_to_meeting_chat` using the
captured `ConversationReference` for that channel. The reference is
captured the first time Bot Framework delivers a channel activity to
`/api/messages`; after that, proactive sends work the same as for
meeting chats.

---

## 6. Operate

The bot publishes every transcript and chat as a versioned envelope
([`alfred-events-v1`](docs/event-contract.md)) to every consumer URL
registered against that channel. Each consumer is one team's backend.
The reference Python sink in this repo subscribes at `POST /events`,
auto-starts a session keyed on `chat_thread_id`, and persists into
both the immutable `raw_ingest_events` audit table and the working
`meeting_events` ledger. The Disney UI reads from this reference
sink: `/m/<chat_thread_id>/status` + live SSE at
`/m/<chat_thread_id>/events`. Open `/` to pick from the active meeting
list.

### Per-channel consumer config (`/api/channels/.../consumers`)

Every channel has its own list of downstream URLs. Manage them on
the bot:

```bash
BOT=https://alfred-disney-bot.eastus.cloudapp.azure.com
TEAM='<team AAD group id>'
CHAN='19:<channel guid>@thread.tacv2'
ENC=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$CHAN")

# List consumers for a channel
curl -sS "$BOT/api/channels/$TEAM/$ENC/consumers" | jq

# Replace the entire list
curl -sS -X PUT "$BOT/api/channels/$TEAM/$ENC/consumers" \
  -H "Content-Type: application/json" \
  -d '{
    "consumers": [
      {
        "name": "alfred-disney-sink",
        "url":  "https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io/events",
        "event_kinds": ["*"]
      },
      {
        "name": "team-b-summarizer",
        "url":  "https://team-b.internal/sink",
        "event_kinds": ["transcript.final","chat.message"],
        "headers": {"X-Team":"B"}
      }
    ]
  }'

# Insert/replace one by name (the rest of the list stays put)
curl -sS -X POST "$BOT/api/channels/$TEAM/$ENC/consumers" \
  -H "Content-Type: application/json" \
  -d '{"name":"team-b-summarizer","url":"https://team-b.internal/sink","event_kinds":["transcript.final"]}'

# Remove one by name
curl -sS -X DELETE "$BOT/api/channels/$TEAM/$ENC/consumers/team-b-summarizer"
```

Bootstrap default: the bot config has an optional
`EventDispatch.BootstrapConsumerUrl`. When set, every newly-attached
channel (and any pre-existing channel with empty consumers and
`legacy_seeded=false`) is auto-seeded with one consumer named
`legacy-default` pointing at this URL. Operators can delete it via
the CRUD API once a real consumer is registered. Disney's
`appsettings.production.json` points it at the Disney sandbox
sink so the channel-attached UI keeps working out of the box.

```bash
SINK=https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io
WEB=https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io
BOT=https://alfred-disney-bot.eastus.cloudapp.azure.com

# Health
curl -sS -m 10 $BOT/api/calling/health
curl -sS -m 10 $SINK/health
curl -sS -m 10 -o /dev/null -w "web HTTP=%{http_code}\n" $WEB

# Sink stats
curl -sS $SINK/stats | jq

# Per-meeting routes (UI uses these — chat_thread_id is the URL key)
TID='19:meeting_xxx@thread.v2'   # URL-encode the colon when needed

curl -sS $SINK/m | jq '.meetings[] | {chat_thread_id, active, total_events}'
curl -sS "$SINK/m/$TID/status"  | jq '.session.meeting_history[] | {kind, role, text}'
curl -sS "$SINK/m/$TID/ledger"  | jq '.events | length'
curl -sS "$SINK/m/$TID/dossier" | jq

# Mute / unmute Alfred for one meeting
curl -sS -X POST "$SINK/m/$TID/mute" -H "Content-Type: application/json" -d '{"muted":true}'

# End a meeting session
curl -sS -X POST "$SINK/m/$TID/end"

# Raw audit (E1) — every inbound event, including drops
SID=$(curl -sS "$SINK/m/$TID/status" | jq -r .session.session_id)
curl -sS "$SINK/sessions/$SID/raw-events" | jq '.events[] | {event_type, dropped_reason}'
curl -sS "$SINK/sessions/$SID/raw-events/export.ndjson" -o "audit-$SID.ndjson"

# Identity (E3) — Teams roster + speaker_id resolutions
curl -sS "$SINK/sessions/$SID/participants"      | jq
curl -sS "$SINK/sessions/$SID/speaker-identity"  | jq
curl -sS -X POST "$SINK/sessions/$SID/speaker-mapping" \
  -H "Content-Type: application/json" -d '{"speaker_id":"speaker_0","aad_object_id":"<AAD>"}'

# Channel rollup — every event (chat / speech / system) under a channel,
# from the channel itself AND every meeting spawned from it, ordered by ts.
TEAM=<aad group id>
CHAN='19:<channel guid>@thread.tacv2'
ENC=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$CHAN")
curl -sS "$SINK/c/$TEAM/$ENC/events?since=2026-05-01T00:00:00Z&kinds=speech,chat" | jq

# Manually link a meeting thread to a channel (also retroactively backfills
# all prior meeting_events / raw_ingest_events for that meeting):
curl -sS -X POST "$SINK/session/link" -H "Content-Type: application/json" \
  -d "$(jq -n --arg tid "19:meeting_xxx@thread.v2" --arg team "$TEAM" --arg chan "$CHAN" \
        '{chat_thread_id:$tid, team_id:$team, channel_id:$chan, channel_thread_id:$chan, source:"manual"}')"
```

### VM operations (canonical pattern: `az vm run-command create`)

`az vm run-command create` is the only sanctioned variant. Never use
`az vm run-command invoke` — the legacy action variant wedges the
extension and forces a manual cleanup over RDP.

```bash
# Tail bot logs
az vm run-command create -g rg-alfred-disney --vm-name vm-alfred-disney --location eastus \
  --run-command-name tail-logs \
  --script 'Get-Content C:\teams-bot-poc\logs\service-output.log -Tail 80; Write-Host "---STDERR---"; Get-Content C:\teams-bot-poc\logs\service-error.log -Tail 40 -ErrorAction SilentlyContinue' \
  --async-execution false --timeout-in-seconds 60 --output none
az vm run-command show -g rg-alfred-disney --vm-name vm-alfred-disney --run-command-name tail-logs \
  --instance-view --query "instanceView.output" -o tsv
az vm run-command delete -g rg-alfred-disney --vm-name vm-alfred-disney --run-command-name tail-logs --yes

# Restart the bot service
az vm run-command create -g rg-alfred-disney --vm-name vm-alfred-disney --location eastus \
  --run-command-name restart-bot \
  --script 'Restart-Service TeamsMediaBot -Force; Start-Sleep -Seconds 8; Get-Service TeamsMediaBot' \
  --async-execution false --timeout-in-seconds 60 --output none
az vm run-command delete -g rg-alfred-disney --vm-name vm-alfred-disney --run-command-name restart-bot --yes

# Per-meeting NDJSON audit logs on disk (written by C# bot)
# Path: C:\teams-bot-poc\meeting-logs\<sanitized_chat_thread_id>\{transcript|chat}.ndjson
az vm run-command create -g rg-alfred-disney --vm-name vm-alfred-disney --location eastus \
  --run-command-name list-audit \
  --script 'Get-ChildItem "C:\teams-bot-poc\meeting-logs" -Recurse -Filter *.ndjson | Select-Object FullName, Length' \
  --async-execution false --timeout-in-seconds 30 --output none
az vm run-command delete -g rg-alfred-disney --vm-name vm-alfred-disney --run-command-name list-audit --yes
```

### Verify the Teams calling webhook points at the VM

Calling MUST terminate at the C# bot host — Container Apps cannot host
`/api/calling` (no Real-Time Media SDK on Linux).

```bash
az bot msteams show -g rg-alfred-disney -n bot-alfred-disney \
  --query "properties.properties.{callingWebhook:callingWebhook, enableCalling:enableCalling}" -o json
```

### Live call-join diagnostic

Use a real, currently running Teams meeting URL. The first response only
proves that the bot API and Graph call-create path accepted or rejected
the request; the actual join/audio path completes asynchronously through
the `/api/calling` webhook and must be confirmed in VM logs.

```bash
BOT=https://alfred-disney-bot.eastus.cloudapp.azure.com
JOIN_URL='<live Teams meeting join URL>'

curl -sS -D - -X POST "$BOT/api/calling/join" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg joinUrl "$JOIN_URL" \
        '{joinUrl:$joinUrl, displayName:"Alfred", joinMode:"invite_and_graph_join", botAttendeePresent:true}')"

curl -sS "$BOT/api/calling/health" | jq
```

Interpretation:

- `200` with `callId` means `communications/calls` accepted the join.
  Immediately tail VM logs and look for `Call added`, `state changed:
  ... -> Established`, `CallHandler created ... audio socket wired`,
  `Transcription started`, and audio frame counters.
- `/api/calling/health` includes `calls[]` with per-call media readiness:
  `readiness`, `readiness_reason`, `unmixed_audio_frames`,
  `primary_mixed_audio_frames`, `recent_peak_sample`, and
  `recent_average_abs_sample`. For Alfred's current speaker-identity path,
  the expected live value is `readiness:"ready"` with
  `unmixed_audio_frames > 0` and `recent_peak_sample > 0`.
- `readiness:"unmixed_audio_missing"` means the bot joined and media frames
  are arriving, but Teams has not supplied `UnmixedAudioBuffers`; treat this
  as a bot/media negotiation failure for the current product path.
- Audio can arrive in two SDK shapes. `receiving unmixed Teams audio
  buffers` means Teams provided per-speaker buffers and MSI hints.
  `receiving primary mixed Teams audio buffers ... FrameBytes=640`
  means Teams provided the mixed 16 kHz PCM frame instead; this is still
  valid audio input for STT, but it has no per-speaker Teams MSI hint.
- `audio level stats ... PeakSample=0, AverageAbsSample=0.0` means the
  bot is joined and media callbacks are firing, but Teams is sending
  silence to the bot. This is not an Azure Speech or sink failure; verify
  Alfred is present in the call roster, the speaker is unmuted in Teams,
  meeting options are not suppressing app/bot audio, and try removing +
  re-adding Alfred or starting a fresh meeting.
- Non-zero `PeakSample` with no `[FINAL]` or `[PARTIAL]` points at the STT
  path after the media socket: check `Azure session started`, transcription
  cancellation logs, and sink `/m/<chat_thread_id>/status`.
- `202` with `deferred:true` means policy auto-invite mode was selected;
  no explicit Graph join was attempted, so Teams must invite the bot via
  the calling webhook.
- `400` with `BOT_NOT_INVITED` means invite mode failed fast because the
  request asserted the bot/service account was not on the meeting invite.
- `403` with `GRAPH_PERMISSION_MISSING` or `TENANT_NOT_ENABLED_FOR_MODE`
  points at RSC/admin-consent or tenant join-mode configuration.
- `502` with `CALL_JOIN_FAILED_7504_OR_7505` points at tenant-level Graph
  calling authorization constraints.
- If the request returns `200` but no async call log lines appear, verify
  the Azure Bot `callingWebhook` still points at the VM and that the VM
  accepts HTTPS traffic for `/api/calling`.

---

## 7. Push a code change

```bash
git push                                   # local main → private/main

# Bot VM (rebuilds C# bot, redeploys + restarts service)
RG_NAME=rg-alfred-disney VM_NAME=vm-alfred-disney \
  TENANT_ID=38387f0b-9a6f-46e2-8373-67422f8c2cb0 \
  APP_SECRET_FILE=/tmp/alfred-disney-app-secret.json \
  VM_ADMIN_PASS_FILE=/tmp/alfred-disney-vm-admin-pass.txt \
  SPEECH_KEY_FILE=/tmp/alfred-disney-speech-key.txt \
  DEPLOY_KEY_FILE=/tmp/alfred-deploy-key \
  BOT_HOSTNAME=alfred-disney-bot.eastus.cloudapp.azure.com \
  MEDIA_HOSTNAME=alfred-disney-bot.eastus.cloudapp.azure.com \
  CERT_FRIENDLY_NAME=alfred-disney-cert CERT_EMAIL=Logan.Robbins@disney.com \
  STT_PROVIDER=AzureSpeech AZURE_SPEECH_REGION=eastus \
  SKIP_REPO_SYNC=0 \
  ./scripts/deploy-azure-vm.sh

# Sink + UI Container Apps (build via ACR, roll the active revision)
TAG=disney-sandbox-$(git rev-parse --short HEAD)
az acr build --registry acralfreddisneye02c0038 --image ca-alfred-api:$TAG --file python/Dockerfile python/
az acr build --registry acralfreddisneye02c0038 --image ca-alfred-web:$TAG --file web/Dockerfile     web/
az containerapp update -n ca-alfred-api -g rg-alfred-disney \
  --image acralfreddisneye02c0038.azurecr.io/ca-alfred-api:$TAG
az containerapp update -n ca-alfred-web -g rg-alfred-disney \
  --image acralfreddisneye02c0038.azurecr.io/ca-alfred-web:$TAG
```

`SKIP_REPO_SYNC=0` is required after pushing new commits. Use
`SKIP_REPO_SYNC=1` for config-only redeploys. The bot service is stopped
before publishing so the running DLL doesn't lock the build.

`scripts/deploy-azure-agent.sh` defaults to a non-Disney resource group;
prefer the explicit `az acr build` + `az containerapp update` flow above
for Disney.

---

## 8. Local dev

```bash
# Sink
cd python && uv sync
uv run python run_variant_sink.py --instance dev --port 8765 \
  --product-spec batcave_platform/specs/alfred.yaml

# UI
cd web && npm install && npm run dev      # http://127.0.0.1:5173

# Tests
cd python && uv run pytest tests -v       # baseline: 121 passed, 2 skipped

# C# build — Docker SDK image with a named NuGet volume so restore is
# fast across runs (~12 min cold, seconds warm). See AGENTS.md §3.3.
docker volume create alfred-nuget-cache
docker run --rm -v "$(pwd):/work" \
  -v alfred-nuget-cache:/root/.nuget/packages \
  -w /work/src mcr.microsoft.com/dotnet/sdk:8.0 \
  dotnet build --configuration Release --nologo /v:m
```

Set `BOT_SEND_CHAT_URL=http://127.0.0.1:3978/api/send-chat` in the sink
env to exercise the real outbound-chat tool path. With it unset, the
tool dry-runs (logs + appends to the ledger, does not POST).

---

## 9. Debug

> Deeper diagnosis playbook (auto-join tiers, PowerShell-via-az gotchas,
> `dotnet publish` stale-binary trap, run-command name caching, etc.)
> lives in [`AGENTS.md`](AGENTS.md) §7. The table below is the
> short-form symptom→fix index.

| Symptom | Fix |
|---|---|
| `/api/calling/health` Healthy but Teams calls don't join | Microsoft RTM media allowlist may be required for the bot AppId. Submit at `https://aka.ms/teams-rtm-onboarding`. Until approved, the bot joins but the media SDK fails to attach to audio. |
| Bot service Running, stderr says `MediaPlatform needs at least 2 cores` | VM has <2 physical cores. Resize: `az vm resize -g rg-alfred-disney -n vm-alfred-disney --size Standard_D4s_v3`, then restart bot service. |
| Bot service Running, stderr says `DllNotFoundException: NativeMedia` | Server-Media-Foundation feature or VC++ Redistributable missing. Re-run `./scripts/deploy-azure-vm.sh`; Phase 1 reinstalls both. |
| `POST /api/messages` → 401 `Invalid AppId passed on token` | `appsettings.production.json` is missing `MicrosoftAppId/Password/Type/TenantId` at the **root** (Bot Framework reads these, distinct from `Bot.*`). Re-run `./scripts/deploy-azure-vm.sh`. |
| Bot crashes on startup with `Certificate with thumbprint '...' not found` | The bot auto-resolves the cert by Subject CN matching `MediaPlatformSettings.ServiceFqdn` (or `CertificateFriendlyName` prefix); restarting is normally enough. If you do see this error, the cert is missing entirely; run `./scripts/deploy-azure-vm.sh`. |
| Sink container revision crashloops with `Product spec file not found at '/app/legionmeet_platform/...'` | Stale env var from pre-rename era. Fix: `az containerapp update -n ca-alfred-api -g rg-alfred-disney --set-env-vars PRODUCT_SPEC_PATH=/app/batcave_platform/specs/alfred.yaml`. |
| `az vm run-command create` Phase 1 fails with `Access to the path 'C:\ProgramData\alfred\deploy_key' is denied` | Old ACL locked SYSTEM to Read-only. The current bootstrap script handles this on re-run; if it still fails, relax the ACL in-place (probe via Run Command: `icacls "C:\ProgramData\alfred\deploy_key" /grant:r "NT AUTHORITY\SYSTEM:F"`). |
| `az vm get-instance-view` returns `vmAgent: null` | ARM cache lag. Probe directly with a Run Command (`Write-Host alive`); if it returns Succeeded, the agent is fine. |
| `Conflict: Run command extension execution is in progress` | Legacy `az vm run-command invoke` wedged the extension. RDP in, remove `C:\Packages\Plugins\Microsoft.CPlat.Core.RunCommandWindows*`, restart `WindowsAzureGuestAgent` + `RdAgent`. |
| Sink `events_received` increments but ledger empty | Auto-start requires non-empty `text` on a `final` transcript or a non-deleted chat. Partials and pre-session chats land in `raw_ingest_events` with a `dropped_reason` instead of in the working ledger — verify via `/sessions/{id}/raw-events`. |
| Call is active, media frames arrive, but transcript stays empty | Tail VM logs for `audio level stats`. `PeakSample=0` and `AverageAbsSample=0.0` means Teams is sending silence to the bot even though the media socket is alive. `PeakSample>0` with no transcript means debug Azure Speech/session cancellation and sink publishing. |
| Ledger shows `speaker_0` instead of real names | C# `Call.Participants.OnUpdated` publisher to `POST /session/participants` is deferred (PROD.md E3). The Python resolver and tables are live; once the C# publisher lands, identities backfill automatically. |
| Transcripts log to `meet-{meetingId}` audit dir but chat logs to `19:meeting_xxx` dir | Joining via short URL (`teams.microsoft.com/meet/...`) yields a synthetic `meet-{meetingId}` thread id for audio; Bot Framework chat carries the real `19:` thread id. Resolution requires a Graph call during join to look up the real thread id from the meeting id. |
| Newly-pushed C# code "deploys" but the running bot still has old behavior | `dotnet publish` is incremental — stale `bin/`+`obj/` from a previous build can produce a fresh-timestamped DLL with old content. Any deploy that touches code must `rm -rf src/bin src/obj` before `dotnet publish`. See `AGENTS.md` §7.4. |
| `az vm run-command create` succeeded but its output is the *previous* attempt's error | Run-command resources are cached by name. Use a unique `--run-command-name` per attempt or `az vm run-command delete` first. See `AGENTS.md` §7.3. |
| Channel rollup `/c/{tid}/{cid}/events` returns nothing for a real meeting | The bot only stamps `channel_id` once it sees a meeting-chat activity carrying `channelData`. If the meeting started but no chat activity has happened yet, post anything in the meeting chat to trigger `/session/link`. Or call `/session/link` manually with the meeting's `chat_thread_id`. |

---

## 10. Deploy key (private repo)

VMs clone the private GitHub repo over SSH using a read-only ed25519
deploy key. Bootstrap drops it on the VM at
`C:\ProgramData\alfred\deploy_key` with a strict ACL.

```bash
# Generate (one-time per dev machine — the public half is already on the repo)
ssh-keygen -t ed25519 -C "alfred-teams-bot deploy" -f /tmp/alfred-deploy-key -N ""
chmod 600 /tmp/alfred-deploy-key

# Register the public half (read-only)
gh api repos/logan-robbins/alfred-teams-bot/keys \
  -f title="$(hostname)-$(date -u +%Y-%m-%d)" \
  -f key="$(cat /tmp/alfred-deploy-key.pub)" \
  -F read_only=true
```

---

## 11. Editing rules (do not violate)

1. **Single canonical meeting ledger** — `InterviewSession.meeting_events`,
   one session per `chat_thread_id`. The immutable layer is
   `raw_ingest_events`; back-link new ledger rows via `source_raw_event_ids`.
2. **`chat_thread_id` is THE meeting key.** Every transcript and chat must
   carry it; the UI must require it in the URL (`/m/<chat_thread_id>`);
   never reintroduce a "current meeting" fallback that lets `/` show a
   dossier.
3. **One inbound chat path** — Bot Framework activities to `/api/messages`,
   then forwarded to the Python `/chat` endpoint.
4. **Outbound Alfred chat** goes through the `send_to_meeting_chat` agent
   tool. The old `teams_chat` output route is gone.
5. **Agent contract** = `AlfredExtraction` (structured output) +
   `send_to_meeting_chat` (sole action). **Do not** reintroduce a
   `SEND/ASK/SILENT` enum.
6. **UI is read-only with respect to the meeting.** Only Alfred speaks
   into chat, only through the tool.
7. **All persistent writes** go through `meeting_agent.persistence.SessionStore`
   so live UI and post-meeting replay read the same truth.
8. **Bot self-resolves its TLS cert at startup** (thumbprint → Subject CN
   match on `MediaPlatformSettings.ServiceFqdn` → `CertificateFriendlyName`
   prefix). Cert auto-renewal must remain transparent.
9. **`python/batcave_platform/specs/alfred.yaml` (`agent.prompt_template`
   and `agent.intervention_policy`) is the sole source of truth** for
   Alfred's instructions and proactivity rules. `AlfredAnalyzer` raises at
   construction if `instructions` is not provided — do not add a fallback
   default in code. Treat both this README and that spec as system
   documents.
10. **One canonical implementation per concern.** No duplicate files, no
    parallel code paths, no `v2` copies, no override-with-fallback
    patterns. If a feature is multi-tenant in spirit, model it as a
    single canonical lookup with one entry today, not a default-plus-override.
11. **Fail fast** when prerequisites are unmet — clear, specific errors at
    system boundaries. Don't add validation or fallbacks for scenarios
    that can't happen.
12. **Channel link integrity.** Stamp `team_id` / `channel_id` /
    `channel_thread_id` at write time when known; backfill prior rows
    via `POST /session/link`. Don't introduce a parallel "channel
    session" model — channel sessions and meeting sessions are both
    `chat_thread_id`-keyed; the link is a side table.
13. **`dotnet publish` always after `rm -rf src/bin src/obj`** in any
    deploy script that touches code. MSBuild's incremental cache will
    happily ship a new-timestamped DLL with old content otherwise. See
    [`AGENTS.md`](AGENTS.md) §7.4.
14. **Manifest changes** require: bump `version` in `manifest.json`,
    regenerate `alfred-sandbox.zip` (`cd manifest && rm
    alfred-sandbox.zip && zip -j alfred-sandbox.zip manifest.json
    color.png outline.png`), re-import in Teams Developer Portal,
    re-grant admin consent if RSCs changed.

---

## 12. What's next (productionalization)

`PROD.md` at the repo root tracks productionalization status. Current
state:

- **E1 raw audit store** ✅ live
- **E2 raw/working ledger split** ✅ live
- **E3 participant identity layer** ✅ Python live; C# `Call.Participants.OnUpdated` publisher deferred
- **E4 explicit proactivity policy** ✅ live
- **E5 multi-sink routing** 🟡 deferred — needs a meeting→sink registration story that survives auto-invite
- **E6 per-meeting URL-routed UI** ✅ live
- **E7 persistent channel attachment** ✅ live — auto-attach on team install + `POST /api/channels/attach`; Graph subscription on `teams/{teamId}/channels/{channelId}/messages` with renewal loop and on-startup restore
- **E8 channel-id metadata + rollup** ✅ live — every event carries `team_id` / `channel_id` / `channel_thread_id`; `POST /session/link` backfills prior rows; `GET /c/{teamId}/{channelId}/events` returns the unified channel timeline

Read `PROD.md` before starting any of the deferred workstreams.
