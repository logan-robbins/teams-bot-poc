# Alfred — Teams Meeting Platform

Microsoft Teams bot that captures audio + chat from meetings and
channels, publishes a versioned event stream
([`docs/event-contract.md`](docs/event-contract.md)) to per-channel
consumer URLs, and mirrors every event into Azure Blob Storage
([`docs/retrieving-transcripts.md`](docs/retrieving-transcripts.md))
for replay. A reference Python sink + the Alfred note-taker agent
ship in this repo as the canonical consumer.

> **AI coding agents:** this README is the **what / where / how**.
> [`AGENTS.md`](AGENTS.md) is the deeper ops manual (debug recipes,
> auto-join tiers, `dotnet publish` traps, etc.). Read this first;
> page over to AGENTS.md when you need to actually fix something.

---

## 1. The 60-second model

```
                            Microsoft Teams
       (meeting chat, group chat, OR persistent channel attachment)
                                  │
   audio PCM + roster   │   Bot Framework activities (/api/messages)
                        │   Graph change notifications (/api/graph-notifications)
                        ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │   C# BOT   src/   ·   Windows VM (vm-alfred-disney)              │
  │                                                                  │
  │   Audio    ──► AzureSpeech ConversationTranscriber (diarized)    │
  │   Chat     ──► Bot Framework + Graph subscription                │
  │   Channel  ──► persistent attach + subscription renewal          │
  │   Joins    ──► auto-join channel meetings + manual /join URL     │
  │   Names    ──► resolved via GraphApiClient using RSCs            │
  │                                                                  │
  │   Every event flows through EventFanoutDispatcher.PublishAsync:  │
  │     ├─► MeetingAuditLogger   (per-thread NDJSON on disk)         │
  │     ├─► BlobEventArchive     (per-channel/meeting .txt blobs)    │
  │     └─► per-channel consumer URLs  (POST /events, retry/queue)   │
  │                                                                  │
  │   Linkage: chat command "@Alfred link to <channel-name>" persists│
  │   a MeetingChannelLink; dispatcher then stamps every event from  │
  │   that meeting with the linked team/channel ids so it rolls up.  │
  └────────────────────┬─────────────────────────────────────────────┘
                       │
                       │  POST  https://{consumer}/events     (HTTP)
                       │  PUT   stalfreddisney/alfred-events  (Blob)
                       ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │   PYTHON SINK   python/   ·   ca-alfred-api Container App       │
  │                                                                  │
  │   /events    →  raw_ingest_events (immutable audit)              │
  │                 + working meeting_events ledger                  │
  │   /m/{id}/*  →  per-session status / dossier / SSE stream       │
  │   /c/{t}/{c}/events → unified channel timeline (chat + meetings) │
  │                                                                  │
  │   AlfredAnalyzer (gpt-5-mini, OpenAI Agents SDK):                │
  │     · runs on every debounced tick                               │
  │     · emits one AlfredExtraction (summary + structured items)    │
  │     · two tools: send_to_meeting_chat, fetch_meeting_transcript  │
  │     · spec: python/batcave_platform/specs/alfred.yaml            │
  └────────────────────┬─────────────────────────────────────────────┘
                       │  SSE + JSON
                       ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │   REACT UI   web/   ·   ca-alfred-web Container App              │
  │     /                    → meeting picker                        │
  │     /m/<chat_thread_id>  → per-meeting dossier (read-only)       │
  │     /channels            → consumer admin + join-any-meeting     │
  │     /channels/inspect/.. → per-channel command center            │
  │     /archive             → blob-archive folder browser           │
  │     /debug               → per-thread NDJSON tail                │
  └──────────────────────────────────────────────────────────────────┘
```

**Single key:** `chat_thread_id`
(`19:meeting_xxx@thread.v2` for a meeting,
`19:{channelId}@thread.tacv2` for a channel). Every event carries
it. Optional `team_id` / `channel_id` / `channel_thread_id` stamp
enables channel-wide rollups without joins.

---

## 2. Where the code lives

| Path | Role | Build | Deploys to |
|---|---|---|---|
| `src/` | C# bot (Graph Communications SDK + Bot Framework) | `dotnet publish -c Release` (.NET 8, win-x64) | `vm-alfred-disney` (`TeamsMediaBot` service) |
| `python/` | FastAPI sink + Alfred agent | `uv sync` | `ca-alfred-api` Container App |
| `python/meeting_agent/` | Canonical session/agent state. `tools.py` defines the two agent tools. | — | — |
| `python/batcave_platform/specs/alfred.yaml` | **Sole source of truth** for Alfred's prompt + intervention policy | — | — |
| `web/` | React 19 + Vite + Tailwind v4 | `npm run build` | `ca-alfred-web` Container App |
| `manifest/` | Teams app manifest (currently **v1.0.11**, 16 RSCs) | `cd manifest && zip alfred-sandbox.zip manifest.json color.png outline.png` | Teams Admin Center / Developer Portal |
| `scripts/deploy-azure-vm.sh` | One-shot bot VM deploy (Phase 1: bootstrap, Phase 2: publish + restart) | — | — |
| `docs/` | Reference docs ([event-contract](docs/event-contract.md), [retrieving-transcripts](docs/retrieving-transcripts.md), STT comparisons, auto-invite setup) | — | — |

---

## 3. Critical paths in each component

### Bot (`src/`)

- **`Services/AlfredBot.cs`** — Bot Framework activity handler. `OnMessageActivityAsync` is the inbound chat hot path. Calls `EnsureChannelAttachedAsync` (idempotent channel attach + display-name enrichment via Graph), `TryHandleMeetingLinkCommandAsync` (`@Alfred link to <channel-name>`), and dispatches a `chat.message` envelope. Auto-join is fire-and-forget inside `TryAutoJoinMeetingChatAsync`.
- **`Services/EventFanoutDispatcher.cs`** — the single outbound path. `PublishAsync(envelope)` stamps meeting links, fires audit → blob → per-consumer queue. Partial-throttle (`EventDispatch:PartialThrottleSeconds`, default 60) caps STT partials per `(thread, speaker)`.
- **`Services/BlobEventArchive.cs`** — anonymous-read blob mirror of every event. System payloads (Teams meeting lifecycle JSON / `<URIObject>`) route to `system.meeting_lifecycle/`; everything else to `{eventKind}/`.
- **`Services/GraphNotificationProcessor.cs`** — handles `teams/{teamId}/channels/{channelId}/messages` notifications. `MaybeAutoJoinChannelMeeting` (call-started detection) and `MaybeFetchPostMeetingTranscript` (transcript-ready trigger from JSON + URIObject system payloads) both live here.
- **`Services/OfficialTranscriptFetcher.cs`** — polls Graph for the post-meeting Microsoft transcript. **Beta endpoint only**; pinned to `https://graph.microsoft.com/beta/appCatalogs/teamsApps/{appId}/installedToOnlineMeetings/...`. Writes `_official-transcript.txt` (clean speaker-per-line) + `_official-transcript.vtt` (raw).
- **`Services/ChannelAttachmentStore.cs`** + **`MeetingChannelLinkStore.cs`** — file-backed registries on the VM (`C:\teams-bot-poc\state\*.json`), reloaded as IHostedServices on startup.
- **`Controllers/CallingController.cs`** — `POST /api/calling/join` (any meeting URL), `/api/calling/health`.
- **`Controllers/DebugController.cs`** — read-only NDJSON tail + `POST /api/debug/fetch-transcript` for manual transcript backfill.

### Sink (`python/`)

- **`transcript_sink.py`** — FastAPI app. `POST /events` is the single ingress; routes by `event_type`. `GET /m/{chat_thread_id}/{status|ledger|dossier|events(SSE)}`. `GET /c/{teamId}/{channelId}/events` is the unified channel timeline.
- **`meeting_agent/session.py`** — `InterviewSessionManager` per `chat_thread_id`. Owns the working ledger + raw audit + dossier.
- **`meeting_agent/agent.py`** — `AlfredAnalyzer`, instantiated per session. One `AlfredExtraction` per debounced tick.
- **`meeting_agent/tools.py`** — `send_to_meeting_chat` (sole outbound) + `fetch_meeting_transcript` (reads `_official-transcript.txt` from the blob archive on demand).
- **`meeting_agent/debounce.py`** — `DEFAULT_QUIET_WINDOW_SECONDS=1.5`, `DEFAULT_MAX_BATCH=8`.

### UI (`web/`)

- **`src/App.tsx`** — routes (`/`, `/m/*`, `/channels`, `/channels/inspect/*`, `/archive`, `/debug`).
- **`src/components/ChannelsAdmin.tsx`** — channel CRUD + join-by-URL panel.
- **`src/components/ChannelCommandCenter.tsx`** — per-channel status, Live chat + Live transcripts + Official transcripts panels.
- **`src/components/ArchiveBrowser.tsx`** — anonymous blob LIST against `stalfreddisney/alfred-events`. Resolves team/channel GUIDs to display names via `bot.listChannels()`.
- **`src/lib/bot.ts`** — typed client for the C# bot's operator API.

---

## 4. Disney environment

| | |
|---|---|
| Subscription | `e02c0038-82c8-4655-9647-38083f301099` (WDI R&D) |
| M365 tenant | `38387f0b-9a6f-46e2-8373-67422f8c2cb0` |
| Resource group | `rg-alfred-disney` (eastus) |
| Bot VM | `vm-alfred-disney` ([`alfred-disney-bot.eastus.cloudapp.azure.com`](https://alfred-disney-bot.eastus.cloudapp.azure.com)) |
| Azure Bot Service | `bot-alfred-disney` (SingleTenant) |
| Bot AppId | `207a38a4-67c5-4ef9-ada8-ea7998734d59` |
| Sink Container App | [`ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io`](https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io) |
| Web Container App | [`ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io`](https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io) |
| ACR | `acralfreddisneye02c0038.azurecr.io` |
| Blob archive | `stalfreddisney/alfred-events` (anonymous public read) |
| Azure OpenAI | `aoai-alfred-disney` (`gpt-5-mini`) |
| Speech Services | `speech-alfred-disney` (S0) |

**Remotes:**
- `private` → `github.com:logan-robbins/alfred-teams-bot.git` (canonical)
- `disney` → `gitlab.wdi.disney.com/Michael.Barron.-ND/teams_integration.git` (branch `alfred-agent-updates` = active MR)
- Never push to `origin` (public mirror — Disney-specific work stays on the two above).

---

## 5. Permissions

The manifest declares **16 RSCs**. Zero tenant-wide Entra app permissions.

**Chat-scoped** (per-meeting / per-chat install):

| Permission | Used for |
|---|---|
| `Calls.JoinGroupCalls.Chat` | Bot enters the call as a participant |
| `Calls.AccessMedia.Chat` | Receive 16 kHz / 16-bit / mono PCM audio |
| `OnlineMeetingParticipant.Read.Chat` | MSI ↔ AAD ↔ display-name lookup |
| `OnlineMeetingTranscript.Read.Chat` | Post-meeting Microsoft transcript (private meetings only — does **not** apply to channel meetings per Microsoft) |
| `OnlineMeetingRecording.Read.Chat` | Reserved |
| `ChatMessage.Read.Chat` | Graph subscription on the meeting chat |
| `ChatMessageReadReceipt.Read.Chat` | Reserved |

**Team-scoped** (persistent channel attachment):

| Permission | Used for |
|---|---|
| `ChannelMessage.Read.Group` | Subscription on `teams/{tid}/channels/{cid}/messages` |
| `ChannelMessage.Send.Group` | Outbound channel send (Bot Framework adapter is the default; Graph is fallback) |
| `ChannelMeeting.ReadBasic.Group` | Discover channel meetings |
| `ChannelMeetingParticipant.Read.Group` | Channel meeting roster |
| `ChannelMeetingTranscript.Read.Group` | Channel meeting transcripts (no documented public GET endpoint uses this — see "Channel meeting transcripts" note in §7) |
| `ChannelMeetingRecording.Read.Group` | Reserved |
| `TeamsAppInstallation.Read.Group` | Install verification |
| `TeamSettings.Read.Group` | `GET /teams/{id}` for team display name |
| `ChannelSettings.Read.Group` | `GET /teams/{id}/channels/{id}` for channel display name |

**Runtime gate outside RSC:** tenant `CsApplicationAccessPolicy` /
`CsTeamsCallingPolicy`. If a join returns `502 CALL_JOIN_FAILED_7504_OR_7505`,
that's a tenant policy, not a permission — Teams admin must grant the
bot's AppId via `Grant-CsApplicationAccessPolicy` and/or
`Set-CsTeamsAppPermissionPolicy`.

**Adding a new RSC requires re-installing on the team** — Teams binds
RSC scopes at install time, so the team must update or reinstall
Alfred for any new scope to take effect.

---

## 6. Deploy

```bash
# After local edits:
git push private main
git push disney main:alfred-agent-updates --force-with-lease

# Bot VM (rebuild C# + restart service via az vm run-command).
# Full example in AGENTS.md §3.4 — the canonical fields are commit SHA,
# rm -rf src/bin src/obj before publish, then Start-Service.

# Sink + UI container apps:
TAG=disney-sandbox-$(git rev-parse --short HEAD)
az acr build --subscription e02c0038-82c8-4655-9647-38083f301099 \
  --registry acralfreddisneye02c0038 --image ca-alfred-api:$TAG \
  --file python/Dockerfile python/
az acr build --subscription e02c0038-82c8-4655-9647-38083f301099 \
  --registry acralfreddisneye02c0038 --image ca-alfred-web:$TAG \
  --file web/Dockerfile web/
az containerapp update --subscription e02c0038-82c8-4655-9647-38083f301099 \
  -n ca-alfred-api -g rg-alfred-disney \
  --image acralfreddisneye02c0038.azurecr.io/ca-alfred-api:$TAG
az containerapp update --subscription e02c0038-82c8-4655-9647-38083f301099 \
  -n ca-alfred-web -g rg-alfred-disney \
  --image acralfreddisneye02c0038.azurecr.io/ca-alfred-web:$TAG
```

**Gotchas (full list in AGENTS.md §7):**
- Always `rm -rf src/bin src/obj` before `dotnet publish`. MSBuild's
  incremental cache ships new-timestamped DLLs with old content.
- Use a unique `--run-command-name` per VM deploy attempt. Names are
  cached; rerunning the same name returns the prior attempt's output.
- VM caps at **25 managed Run Commands**. Prune Succeeded ones
  periodically — full deploys silently fail with `BadRequest` then
  appear as `ResourceNotFound` on the new name.

---

## 7. Debug

```bash
BOT=https://alfred-disney-bot.eastus.cloudapp.azure.com
SINK=https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io

# Health
curl -sS $BOT/api/calling/health | jq
curl -sS $SINK/health

# Operator API
curl -sS $BOT/api/channels | jq                    # attachments
curl -sS $BOT/api/debug/transcripts | jq           # NDJSON audit listing
curl -sS "$BOT/api/debug/transcripts/<sanitized_id>?kind=chat&tail=50" | jq

# Per-meeting routes (UI uses these — chat_thread_id keyed)
curl -sS "$SINK/m/<chat_thread_id>/status"  | jq
curl -sS "$SINK/m/<chat_thread_id>/ledger"  | jq
curl -sS "$SINK/m/<chat_thread_id>/dossier" | jq

# Manual transcript backfill
curl -sS -X POST "$BOT/api/debug/fetch-transcript" -H 'Content-Type: application/json' \
  -d '{"call_id":"...","organizer_oid":"...","team_id":"...","channel_id":"...","channel_thread_id":"..."}'
```

**Channel meeting transcripts:** Microsoft documents
`OnlineMeetingTranscript.Read.Chat` as "applies only to scheduled
private chat meetings, not to channel meetings." There is no public
GET endpoint that consumes `ChannelMeetingTranscript.Read.Group`
under RSC alone. To get a transcript for a channel meeting without
org-wide consent: schedule it as a **private** meeting, install
Alfred in the meeting chat, and use `@Alfred link to <channel-name>`
to roll the resulting events up under the channel.

**See `AGENTS.md` §7** for the full symptom→fix index (auto-join
tiers, `vmAgent: null` ARM lag, stale `dotnet publish`, `7504/7505`
calling policy, etc.).

---

## 8. Editing rules (do not violate)

1. **Single canonical meeting ledger** — `InterviewSession.meeting_events`,
   one session per `chat_thread_id`. Immutable layer is
   `raw_ingest_events`; back-link new ledger rows via
   `source_raw_event_ids`.
2. **`chat_thread_id` is THE meeting key.** Every transcript and chat
   carries it; UI URL requires it. Never reintroduce a "current
   meeting" fallback.
3. **One inbound chat path** — Bot Framework `/api/messages` → C#
   bot publishes via `EventFanoutDispatcher`. No parallel ingress.
4. **Outbound Alfred chat** goes through the
   `send_to_meeting_chat` tool. The old `teams_chat` output route is
   gone.
5. **Agent contract** = `AlfredExtraction` (structured output) +
   `send_to_meeting_chat` + `fetch_meeting_transcript`. **Do not**
   reintroduce a `SEND/ASK/SILENT` enum.
6. **`alfred.yaml` is the sole source of truth** for Alfred's prompt
   and intervention policy. `AlfredAnalyzer` raises at construction
   if `instructions` is missing — do not add a code fallback.
7. **One canonical implementation per concern.** No duplicate files,
   no parallel paths, no `v2` copies. If a feature is multi-tenant
   in spirit, model it as a single lookup with one entry today.
8. **Fail fast.** Clear, specific errors at system boundaries. No
   validation for impossible scenarios.
9. **Channel link integrity.** Stamp `team_id` / `channel_id` /
   `channel_thread_id` at write time when known; backfill prior rows
   via `POST /session/link` or the
   `MeetingChannelLink` chat command. Don't introduce a parallel
   "channel session" model — channel and meeting sessions are both
   `chat_thread_id`-keyed.
10. **`dotnet publish` always after `rm -rf src/bin src/obj`** in any
    deploy script that touches code. See AGENTS.md §7.4.
11. **Manifest changes** require: bump `version`, regenerate
    `alfred-sandbox.zip`, re-import in Teams Developer Portal,
    re-grant admin consent if RSCs changed. **New RSCs require team
    re-install** — Teams binds RSC scopes at install time.
12. **Push protocol** — only `private` and `disney` remotes. Never
    push to `origin`.

---

## 9. Pointers

- [`AGENTS.md`](AGENTS.md) — deep ops manual (build internals, debug
  recipes, deploy mechanics, gotchas).
- [`docs/event-contract.md`](docs/event-contract.md) —
  `alfred-events-v1` envelope schema + per-`event_type` payload.
- [`docs/retrieving-transcripts.md`](docs/retrieving-transcripts.md) —
  blob archive layout + Python / curl / az recipes.
- [`docs/STT-PROVIDER-COMPARISON.md`](docs/STT-PROVIDER-COMPARISON.md) /
  [`docs/STT-SHORTLIST.md`](docs/STT-SHORTLIST.md) — provider tradeoffs.
- [`docs/TEAMS-AUTO-INVITE-SETUP.md`](docs/TEAMS-AUTO-INVITE-SETUP.md) —
  one-time tenant setup for auto-invite mode.
- `PROD.md` — productionalization tracker.
