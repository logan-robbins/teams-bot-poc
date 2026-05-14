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
  │     ├─► BlobEventArchive   (per-event JSON blobs in Blob Storage) │
  │     └─► per-channel consumer URLs  (POST, retry/queue)           │
  │                                                                  │
  │   Schema: alfred-v2. Two event families:                         │
  │     channel.*   → ChannelRef { team_id, channel_id, thread_id }  │
  │     meeting.*   → MeetingRef { meeting_id, channel_link? }       │
  │                                                                  │
  │   Linkage: "@Alfred link to <channel>" persists a channel_link   │
  │   on MeetingRef. All subsequent meeting events carry the link.   │
  └────────────────────┬─────────────────────────────────────────────┘
                       │
                       │  POST  https://{consumer}/v2/events  (HTTP)
                       │  PUT   stalfreddisney/alfred-events   (Blob)
                       ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │   PYTHON SINK   python/   ·   ca-alfred-api Container App        │
  │   (⚠ in-progress rewrite to v2 — currently running v1 schema)    │
  │                                                                  │
  │   POST /v2/events  →  single ingest for all event types          │
  │   GET  /v2/teams/{tid}/channels/{cid}/threads/…  hierarchical    │
  │   GET  /v2/meetings/{mid}/…                       reads          │
  │   GET  /v2/resolve?kind=meeting&subject=…         name lookup    │
  │   GET  /v2/index                                  index files    │
  │                                                                  │
  │   AlfredAnalyzer (claude-haiku-4-5, Anthropic Agents SDK):       │
  │     · runs on every debounced tick                               │
  │     · tools: send_to_meeting_chat, fetch_meeting_transcript,     │
  │              list_meetings, resolve_meeting_by_name              │
  │     · spec: python/batcave_platform/specs/alfred.yaml            │
  └────────────────────┬─────────────────────────────────────────────┘
                       │  SSE + JSON
                       ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │   REACT UI   web/   ·   ca-alfred-web Container App              │
  │   (⚠ in-progress rewrite to v2 — currently pointing at v1 API)   │
  │     /                    → meeting picker (by subject)           │
  │     /m/<meeting_id>      → per-meeting dossier (read-only)       │
  │     /channels            → consumer admin + join-any-meeting     │
  │     /channels/inspect/.. → per-channel command center            │
  │     /archive             → blob-archive folder browser           │
  └──────────────────────────────────────────────────────────────────┘
```

**Two canonical keys, mirroring the Microsoft Graph URL hierarchy:**

```
Team (team_id)
  └── Channel (team_id, channel_id)
        └── Thread (thread_id = root message id)
              └── Messages / Attachments

Meeting (meeting_id = Graph onlineMeeting id)
  ├── Chat (meeting_chat_thread_id) → Messages / Attachments
  ├── Transcripts → partial / final / official VTT
  └── channel_link? → optional back-reference to (team_id, channel_id)
```

**Channel meetings have no audio.** The bot lacks `Calls.AccessMedia`
at team scope. A meeting inside a channel shows up only as
`channel.message.*` events. The `meeting.*` event family exists only
for **private meetings the bot was added to via `+ Apps`**.

**The channel-link problem.** Microsoft Graph does not natively tie a
meeting to a channel. Alfred bridges this via: (a) Bot Framework
`channelData` when the Teams client tells us which channel spawned the
meeting, (b) the `@Alfred link to <channel-name>` chat command, or (c)
`GraphMetadataResolver` lookups at join time. The `channel_link` on
`MeetingRef` is the result; once set, it rides on every subsequent
event for that meeting.

---

## 2. Two tenants — read this before debugging permissions

This deployment straddles two Entra / Teams tenants with very
different control:

| Tenant | What lives here | Our role |
|---|---|---|
| **`plutosdoghouse.com`** (id `38387f0b-...`) — the **Sandbox** Teams + Entra tenant where Alfred is installed as a Teams app, where meetings happen, and where chat flows | Teams manifest install, RSC grants on each install, the actual Teams meetings + channels the bot listens to | **Tenant member only** — we are **not** Entra / Teams admins here. We **cannot** run `Grant-CsApplicationAccessPolicy`, `New-CsApplicationAccessPolicy`, or grant any tenant-wide Entra app permissions. Everything we get is RSC-scoped at install time. |
| **`disney.com` → WDI R&D subscription `e02c0038-...`** — the Azure subscription where the **infrastructure** runs | C# bot VM, Container Apps (sink + UI), ACR, Azure OpenAI, Speech Services, Storage account `stalfreddisney`, Bot Service registration | **Subscription Contributors.** We can deploy anything, change env vars, build images, restart services. **No** Entra-admin rights even here — Azure AD app-registration **Owners** can edit the Alfred app registration (`207a38a4-...`); other Entra-admin actions need a separate principal. |

Concrete consequences:
- **Anything that needs a tenant policy grant in Sandbox is out of
  reach.** That includes `CsApplicationAccessPolicy` (the
  7504/7505 unlock) and `OnlineMeetingTranscript.Read.All`. We
  always have to find an RSC-only path or work around it.
- **The bot's App Registration owner** in WDI Entra determines the
  "via {UPN}" parenthetical Teams shows next to Alfred's chat
  messages. Change it via Azure Portal → Entra ID → App
  registrations → Owners (WDI admins can; Sandbox admins cannot).
- **Manifest upload + admin consent** happens in the **Sandbox**
  tenant. We submit the zip; a Sandbox admin (e.g., Michael
  Barron) approves it. Re-uploading a new manifest version
  requires that same admin's approval. Adding a new RSC further
  requires every existing team installation to **update** the app.

---

## 3. Where the code lives

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

### 7.1 Call-join failures (the most common, most-frustrating bug)

When a join attempt fails, the error code tells you which **tier** is
blocking. There are three layers in order of likelihood for our
Sandbox-tenant constraints:

| Error / code | Tier blocked | What it actually means | What to do |
|---|---|---|---|
| `502 CALL_JOIN_FAILED_7504_OR_7505` | **Tenant calling policy** | `CsApplicationAccessPolicy` does not allow Alfred's AppId (`207a38a4-...`) to act on Graph Communications calls. This is the most common failure mode in Sandbox. | **Out of reach for us.** Requires a Sandbox Teams admin to run `New-CsApplicationAccessPolicy` + `Grant-CsApplicationAccessPolicy`. Until granted, no bot can join any call on this tenant. |
| `403 GRAPH_PERMISSION_MISSING` | **RSC scope** | Either the manifest version on the team is older than the one with the needed scope, OR the team hasn't been re-installed since a new RSC was added. | Verify manifest version on the install: Teams Admin Center → Manage apps → Alfred Sandbox. If old, click **Update** on every team install. Confirm with `curl $BOT/api/channels` — the `auto_join_enabled` row will show. |
| `403 TENANT_NOT_ENABLED_FOR_MODE` | **Tenant meeting policy** | `CsTeamsMeetingPolicy` blocks the requested join mode (most commonly app-as-attendee `invite_and_graph_join` is disabled). | Sandbox admin must allow the mode. Workaround: switch to `policy_auto_invite` join mode if the bot is on the meeting invite. |
| `400 BOT_NOT_INVITED` | **App-as-attendee invariant** | The C# join workflow's `BotAttendeePresent=true` assertion failed — no service-account row in the meeting roster matching the bot. | Either set `BotAttendeePresent=false` (relies purely on Graph join), or actually invite the bot's service account to the meeting. |
| `200` then no audio frames arrive | **Microsoft RTM media allowlist** | The bot reaches the meeting via Graph but Teams refuses to wire the media socket. The Real-Time Media SDK requires per-bot allowlisting by Microsoft. | Submit at `https://aka.ms/teams-rtm-onboarding` with Alfred's AppId. Tail VM logs: `Call added` then no `Established` / no `audio frame` lines = waiting on Microsoft. |
| `Audio socket up`, `PeakSample=0`, no transcript | **Meeting silence** | Bot joined cleanly, Teams is sending frames, but the audio buffer is silence. Speaker may be muted client-side, or meeting options suppress app audio. | Confirm Alfred is in the roster; have a human unmute and speak; consider removing + re-adding Alfred. Not a code bug. |

### 7.2 Why a channel meeting transcript never lands

Microsoft documents `OnlineMeetingTranscript.Read.Chat` as
"**applies only to scheduled private chat meetings, not to channel
meetings.**" There is no public GET endpoint that consumes our
`ChannelMeetingTranscript.Read.Group` RSC alone. Workaround: schedule
the meeting as a **private** meeting (not channel), add Alfred to the
meeting chat via `+ Apps`, then post `@Alfred link to <channel-name>`
in the meeting chat. From then on every event from that meeting
rolls up under the named channel.

### 7.3 General health probes

```bash
BOT=https://alfred-disney-bot.eastus.cloudapp.azure.com
SINK=https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io
SA=https://stalfreddisney.blob.core.windows.net/alfred-events

curl -sS $BOT/api/calling/health | jq            # bot media readiness
curl -sS $SINK/health                            # sink

curl -sS $BOT/api/channels | jq                  # channel attachments + last_auto_join_attempt

# Blob archive index — what meetings Alfred has ever seen
curl -sS "$SA/indexes/meetings.json" | jq 'to_entries | map({id:.key,subject:.value.subject})'

# Meeting transcript (official VTT after meeting ends)
MID="<meeting_id>"
curl -sS "$SA/meetings/$MID/transcripts/official.vtt"

# Manual transcript backfill (when the auto-trigger missed a meeting)
# meeting_chat_thread_id is optional; call_id and organizer_oid are required
curl -sS -X POST "$BOT/api/debug/fetch-transcript" -H 'Content-Type: application/json' \
  -d '{"call_id":"...","organizer_oid":"...","meeting_chat_thread_id":"..."}'
```

**See `AGENTS.md` §7** for the full symptom→fix index (auto-join
tiers, `vmAgent: null` ARM lag, stale `dotnet publish`, run-command
name caching, etc.).

---

## 8. Editing rules (do not violate)

1. **`meeting_id` is the canonical meeting key** — the Graph
   `onlineMeeting` id (URL-safe base64). `meeting_chat_thread_id` is a
   sub-resource. Never use `chat_thread_id` as a surrogate meeting key.
2. **Discriminated envelope** — every `AlfredEventEnvelope` has exactly
   one of `ChannelRef` or `MeetingRef` populated, never both, never
   neither. The discriminator is the `event_type` prefix (`channel.*`
   → `ChannelRef`; `meeting.*` → `MeetingRef`).
3. **No v1 shims.** Do not add "if old schema, do X else Y" branches.
   `alfred-v2` is the only schema the bot emits. v1 is dead.
4. **Channel meetings have no `meeting.*` events.** They surface only
   as `channel.message.*` on the channel thread. Do not model channel
   meetings as first-class `Meeting` entities.
5. **One inbound chat path** — Bot Framework `/api/messages` → C#
   bot publishes via `EventFanoutDispatcher`. No parallel ingress.
6. **Outbound Alfred chat** goes through the `send_to_meeting_chat`
   tool only.
7. **`alfred.yaml` is the sole source of truth** for Alfred's prompt
   and intervention policy. `AlfredAnalyzer` raises at construction
   if `instructions` is missing — do not add a code fallback.
8. **One canonical implementation per concern.** No duplicate files,
   no parallel paths, no `v2` copies.
9. **Fail fast.** Clear, specific errors at system boundaries. No
   validation for impossible scenarios.
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
- [`docs/TEAMS-AUTO-INVITE-SETUP.md`](docs/TEAMS-AUTO-INVITE-SETUP.md) —
  one-time Sandbox-tenant admin setup for auto-invite mode.
