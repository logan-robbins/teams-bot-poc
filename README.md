# Alfred

Microsoft Teams meeting assistant for The Walt Disney Company. Joins a
Teams meeting, captures diarized audio + chat, runs an LLM agent that
maintains a live dossier (decisions, open questions, action items, risks)
keyed per-meeting, and posts back into the meeting chat under explicit
intervention rules.

This README is the operational source of truth. Read it end-to-end before
touching code. `PROD.md` tracks productionalization status and the design
notes for deferred work.

---

## 1. System

```
                    ┌──────────────────────────────┐
                    │    Microsoft Teams meeting   │
                    │   (audio + chat + roster)    │
                    └──────────────┬───────────────┘
                                   │
                  audio PCM        │   Bot Framework chat activities
                  + participants   │   (/api/messages)
                                   ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │     C# bot  (src/, Windows VM, Graph Communications SDK)         │
   │  - Joins the call; streams PCM to AzureConversationTranscriber   │
   │    (the only sanctioned STT on Disney; emits diarized speaker_N) │
   │  - Reads unmixed per-speaker buffers when Teams sends them;      │
   │    otherwise reads the primary mixed PCM buffer                  │
   │  - Per audio buffer: snapshots ActiveSpeakers + DominantSpeaker  │
   │    MediaSourceIds; stamped onto every TranscriptEvent (E3)       │
   │  - Reads ICall.Participants → MSI ↔ AAD ↔ display_name           │
   │  - Stamps chat_thread_id on every event                          │
   │  - Per-meeting NDJSON audit log on disk (see §4)                 │
   │  - Sends Alfred's outbound chat via Bot Framework adapter        │
   └────────────────────────┬─────────────────────────────────────────┘
                            │
       POST /transcript     │   POST /chat
       POST /session/        │   POST /api/send-chat   ◀── reverse path
            participants    │
                            ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │     Python sink  (python/, FastAPI on Container Apps)            │
   │                                                                   │
   │  Ingest layer (immutable):                                        │
   │    raw_ingest_events — every inbound event, hashed, BEFORE any   │
   │      filter (partial drop, session-active, echo suppression)     │
   │                                                                   │
   │  Working layer (cleanable):                                       │
   │    SessionRegistry — one InterviewSessionManager per             │
   │      chat_thread_id; auto-starts on first event                  │
   │    meeting_events — normalized ledger; back-links via            │
   │      source_raw_event_ids to the immutable raw rows              │
   │    ParticipantResolver — MSI → AAD; manual > teams_msi_unique >  │
   │      teams_msi_group > sole_human > unresolved (E3)              │
   │                                                                   │
   │  Agent layer:                                                     │
   │    AlfredAnalyzer — debounced, one AlfredExtraction per tick     │
   │      (rolling summary, topics, decisions, open_questions,        │
   │       action_items, risks; merged by id)                         │
   │    Intervention policy — explicit rules in alfred.yaml +         │
   │      45s cooldown + directly-addressed bypass (E4)               │
   │    send_to_meeting_chat — sole outbound action surface           │
   │                                                                   │
   │  SQLite tables: sessions, meeting_events, raw_ingest_events,     │
   │    meeting_participants, participant_msi_bindings,               │
   │    speaker_identity_links, extractions, tool_calls, dossier_items│
   │                                                                   │
   │  SSE /m/{chat_thread_id}/events drives the UI                    │
   └────────────────────────┬─────────────────────────────────────────┘
                            │ SSE + JSON, filtered by chat_thread_id
                            ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │     React UI  (web/, Vite + React 19 + react-router-dom)         │
   │   /             → MeetingList (picker, polls /m every 2s)        │
   │   /m/<id>       → MeetingDossier (3 cols: Ledger / Dossier /     │
   │                    Companion Rail). Read-only — only Alfred      │
   │                    speaks into chat, only via the tool.          │
   └──────────────────────────────────────────────────────────────────┘
```

**Meeting key:** `chat_thread_id` (e.g. `19:meeting_xxx@thread.v2`) is the
canonical id for a Teams meeting and the only stable identifier present
on both audio and chat paths. Knowing the `chat_thread_id` IS the access
boundary for the UI — there is no other auth on `/m/<id>`.

**Agent contract (do not change without reading §6):** the analyzer emits
one `AlfredExtraction` per tick (rolling summary + topics + notes +
decisions + open_questions + action_items + risks, merged by `id`) and
optionally calls one tool — `send_to_meeting_chat(text, kind, ...)`.
There is no `SEND/ASK/SILENT` enum. Silence is "did not call the tool".

**Chunking / debouncing contract:** C# does not debounce Alfred. C# forwards
Teams audio into STT, then POSTs every STT event to Python `/transcript`;
it also POSTs every meeting-chat event to Python `/chat`. Python owns the
working ledger and Alfred batching. `partial` transcripts are raw-audited
but not promoted to the working ledger; `final` transcripts and chat
messages can trigger Alfred. The agent loop debounces in
`python/meeting_agent/debounce.py` with `DEFAULT_QUIET_WINDOW_SECONDS=1.5`
and `DEFAULT_MAX_BATCH=8`, used by `python/transcript_sink.py`.

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

When Alfred is installed into a Teams meeting/chat the installer grants
**five chat-scoped Resource-Specific Consent permissions**, scoped to that
single chat. There are zero tenant-wide Graph permissions.

| Permission | Used for |
|---|---|
| `Calls.JoinGroupCalls.Chat` | Bot enters the call as a participant. |
| `Calls.AccessMedia.Chat` | Receive 16 kHz / 16-bit / mono PCM audio. |
| `OnlineMeetingParticipant.Read.Chat` | MSI ↔ AAD ↔ display-name lookup (E3). |
| `ChatMessage.Read.Chat` | Reserved for the Graph notification chat path (PROD.md). |
| `ChatMessageReadReceipt.Read.Chat` | Reserved (planned engagement signal). |

Plus two manifest-level Teams app permissions: `identity`,
`messageTeamMembers`. The manifest also requests `supportsCalling: true`.

If the tenant treats Alfred as "unverified publisher", an M365 admin must
grant org-wide consent in **Teams Admin Center → Manage apps →
Permissions**. The manifest cannot bypass this.

---

## 4. Repo layout

| Folder | Role |
|---|---|
| `src/` | C# Teams bot. Graph Communications SDK + Bot Framework. Builds with `dotnet build`. |
| `python/` | FastAPI sink + Alfred agent. Run with `uv run python run_variant_sink.py …` from this folder. |
| `python/meeting_agent/` | Canonical session/agent state. `models.py`, `session.py`, `persistence.py`, `agent.py`, `tools.py`, `identity.py`. |
| `python/batcave_platform/` | Product spec loader + output routes. `specs/alfred.yaml` is the **sole source of truth** for Alfred's prompt and intervention policy. |
| `python/tests/` | `uv run pytest tests` baseline: 118 passed, 2 skipped. |
| `web/` | React 19 + Vite + Tailwind v4 UI. Per-meeting routing via `react-router-dom`. |
| `manifest/` | Teams app manifest (`manifest.json`, `alfred-sandbox.zip`). Bot AppId, RSC permissions, valid domains. |
| `scripts/` | Deploy + ops scripts. Canonical entrypoints: `deploy-azure-vm.sh`, `deploy-azure-agent.sh`, `bootstrap-production-vm.ps1`, `join_meeting.sh`. |

**Canonical state object:** `InterviewSession` in
`python/meeting_agent/models.py`. **Canonical history:**
`InterviewSession.meeting_events` (single append-only ledger of normalized
speech + chat + system events). One session per `chat_thread_id`, held in
`SessionRegistry`. The immutable raw layer is `raw_ingest_events`;
`MeetingEvent.source_raw_event_ids` back-links every working-ledger row
to the raw rows that produced it.

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

The bot stamps every transcript and chat with the meeting's
`chat_thread_id` and POSTs them to `/transcript` / `/chat` on the sink.
The sink's `SessionRegistry` auto-starts a session keyed on that id and
persists everything into both the immutable `raw_ingest_events` audit
table and the working `meeting_events` ledger. The UI reads
`/m/<chat_thread_id>/status` + live SSE at `/m/<chat_thread_id>/events`.
Open `/` to pick from the active meeting list.

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
cd python && uv run pytest tests -v       # baseline: 118 passed, 2 skipped

# C# build
dotnet build
```

Set `BOT_SEND_CHAT_URL=http://127.0.0.1:3978/api/send-chat` in the sink
env to exercise the real outbound-chat tool path. With it unset, the
tool dry-runs (logs + appends to the ledger, does not POST).

---

## 9. Debug

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

Read `PROD.md` before starting any of the deferred workstreams.
