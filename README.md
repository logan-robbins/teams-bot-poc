# Alfred — Teams Meeting Platform

Microsoft Teams bot. Captures audio + chat from meetings and channels. Publishes a versioned event stream ([`docs/event-contract.md`](docs/event-contract.md)) to per-channel consumer URLs. Mirrors every event into Azure Blob Storage ([`docs/retrieving-transcripts.md`](docs/retrieving-transcripts.md)) for replay. The reference Python sink + Alfred note-taker agent ship in this repo as the canonical consumer.

For deeper ops detail (debug recipes, auto-join tiers, `dotnet publish` traps), see [`AGENTS.md`](AGENTS.md).

---

## 1. The 60-second model

```
                         Microsoft Teams
       meeting chat / group chat / persistent channel attachment
                                  |
              +-----------------------------------------------+
              | Graph media frames (audio + roster)           |
              | Bot Framework activities (/api/messages)      |
              | Graph notifications (/api/graph-notifications)|
              +-----------------------+-----------------------+
                                      |
                                      v
+------------------------------------------------------------------------+
| C# BOT = PLATFORM RAILS                                                |
| src/ on Windows VM vm-alfred-disney                                    |
|                                                                        |
| Captures: audio PCM + roster, chat activities, Graph channel changes   |
| Resolves: names, channel links, meeting metadata                       |
| Emits: one alfred-v2 envelope per event                                |
|                                                                        |
| PublishAsync(envelope) does the split:                                 |
|   1. local NDJSON audit on the VM                                      |
|   2. PUT blob archive JSON                                             |
|      stalfreddisney/alfred-events/...                                  |
|   3. POST the same envelope to each enabled consumer URL               |
|      default: https://ca-alfred-api.../v2/events                       |
|      client:  https://client-service.../v2/events                      |
|                                                                        |
| Blob writes and HTTP consumer posts are sibling outputs. The bot does  |
| not post to only one endpoint, and blob storage is not written by the  |
| Python sink. A slow or broken consumer does not stop archive writes.   |
+-----------------------------+------------------------------------------+
                              |
              +---------------+----------------+
              |                                |
              v                                v
+-----------------------------+--+    +-----------------------------------+
| PLATFORM INTERFACE             |    | CONSUMER IMPLEMENTATIONS          |
| web/ on ca-alfred-web          |    |                                   |
|                                |    | Our Alfred: python/ on            |
| /channels: attach channels,    |    | ca-alfred-api                     |
| register consumer URLs,        |    | POST /v2/events -> ledger ->      |
| toggle auto-join               |    | AlfredAnalyzer -> send-chat       |
|                                |    |                                   |
| /archive: browse blob archive  |    | Client Alfred: server_v2.py or    |
| /m/<meeting_id>: read our      |    | a service Michael owns            |
| Alfred sink's dossier          |    | POST /v2/events and/or blob       |
|                                |    | polling -> their agent ->         |
| This is part of the rails. It  |    | send-chat if they want to speak   |
| configures and inspects the    |    +-----------------------------------+
| platform; it is not a custom   |
| downstream agent.              |
+--------------------------------+
```

The boundary is intentional: **`src/` plus the `web/` operator interface are the platform rails**, while **`python/` is our Alfred implementation**. A client who wants their own Alfred should implement a consumer like `server_v2.py` or register their own service URL; they do not need to fork the C# bot or the Python sink unless they are changing the platform itself.

**Two canonical keys, mirroring Microsoft Graph's URL hierarchy:**

```
Team (team_id)
  └── Channel (team_id, channel_id)
        └── Thread (thread_id = root message id)
              └── Messages / Attachments

Meeting (meeting_id = chat thread id of the meeting, 19:meeting_<base64>@thread.v2)
  ├── Chat → Messages / Attachments
  ├── Transcripts → partial / final / official VTT
  └── channel_link? → optional back-reference to (team_id, channel_id)
```

**`meeting_id` is the Teams chat thread id of the meeting** (`19:meeting_<base64>@thread.v2`). The Graph `onlineMeeting.id` is a separate Graph-side identifier that the system does not key on.

**Channel meetings have no audio.** The bot lacks `Calls.AccessMedia` at team scope. A meeting inside a channel surfaces only as `channel.message.*` events. The `meeting.*` family exists only for private meetings the bot was added to via `+ Apps`.

**The channel-link problem.** Microsoft Graph does not natively tie a meeting to a channel. Alfred bridges this via (a) Bot Framework `channelData` when the Teams client tells us which channel spawned the meeting, (b) the `@Alfred link to <channel-name>` chat command, or (c) Graph metadata resolution at join time. Once `MeetingRef.channel_link` is set, it rides on every subsequent event.

---

## 2. Two tenants — read this before debugging permissions

| Tenant | What lives here | Our role |
|---|---|---|
| **`plutosdoghouse.com`** (id `38387f0b-...`) — Sandbox Teams + Entra tenant where Alfred is installed | Teams manifest install, RSC grants on each install, the actual Teams meetings + channels | **Tenant member only.** Not Entra / Teams admins. Cannot run `Grant-CsApplicationAccessPolicy`, `New-CsApplicationAccessPolicy`, or grant any tenant-wide Entra app permissions. Everything is RSC-scoped at install time. |
| **`disney.com` → WDI R&D `e02c0038-...`** — Azure subscription with the infrastructure | C# bot VM, Container Apps (sink + UI), ACR, Azure OpenAI, Speech Services, `stalfreddisney`, Bot Service registration | **Subscription Contributors.** Can deploy, change env vars, build images, restart services. App-registration Owners on the Alfred app (`207a38a4-...`) can edit it; other Entra-admin actions need a separate principal. |

Concrete consequences:
- Anything that needs a tenant-wide grant in Sandbox is out of reach: tenant-wide Graph `Calls.*` application permissions, `CsApplicationAccessPolicy` changes, Microsoft Cloud Communications app-hosted-media enablement. Prefer RSC-only paths; route admin/support through Sandbox.
- The bot's App Registration owner in WDI Entra determines the "via {UPN}" parenthetical Teams shows next to Alfred's chat messages.
- Manifest upload + admin consent happens in the Sandbox tenant. A Sandbox admin (e.g., Michael Barron) approves the zip. Adding a new RSC also requires every existing team installation to update the app.

---

## 3. Where the code lives

| Path | Role | Build | Deploys to |
|---|---|---|---|
| `src/` | C# bot (Graph Communications SDK + Bot Framework) | `dotnet publish -c Release` (.NET 8, win-x64) | `vm-alfred-disney` (`TeamsMediaBot` service) |
| `python/` | FastAPI sink + Alfred agent | `uv sync` | `ca-alfred-api` Container App |
| `python/meeting_agent/` | Canonical session/agent state; tool definitions | — | — |
| `python/batcave_platform/specs/alfred.yaml` | Sole source of truth for prompt + intervention policy | — | — |
| `python/intent.py` | Lightweight Intent Alignment consumer prototype (`/v2/events` + sample indexed sources + JSONL memories) | `uv run uvicorn intent:app --port 8765` | local/container prototype |
| `web/` | React 19 + Vite + Tailwind v4 | `npm run build` | `ca-alfred-web` Container App |
| `manifest/` | Teams app manifest (currently v1.0.12, 16 RSCs) | `cd manifest && zip alfred-sandbox.zip manifest.json color.png outline.png` | Teams Admin Center / Developer Portal |
| `scripts/deploy-azure-vm.sh` | One-shot bot VM deploy (bootstrap + publish + restart) | — | — |
| `docs/` | Reference docs (event contract, data retrieval, auto-invite setup) | — | — |

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
| Sink Container App | [`ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io`](https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io) (depends on `pg-alfred-disney`) |
| Web Container App | [`ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io`](https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io) |
| ACR | `acralfreddisneye02c0038.azurecr.io` |
| Blob archive | `stalfreddisney/alfred-events` (anonymous public read) |
| PostgreSQL | `pg-alfred-disney` (Azure Database for PostgreSQL Flexible Server, `Standard_B1ms`, eastus2, db `alfred`, PG 16, 32 GB) |
| Sink file share (dormant) | `stalfreddisney/alfred-sink-data` — historical sqlite mount, no longer used (§6 / §7) |
| Azure OpenAI | `aoai-alfred-disney` (`gpt-5-mini`) |
| Speech Services | `speech-alfred-disney` (S0) |

**Remotes:**
- `private` → `github.com:logan-robbins/alfred-teams-bot.git` (canonical)
- `disney` → `gitlab.wdi.disney.com/Michael.Barron.-ND/teams_integration.git` (branch `alfred-agent-updates` = active MR)
- Never push to `origin` (public mirror).

---

## 5. Permissions — complete list for a fully functional Alfred

Everything below is **required** for full functionality. Status legend: ✅ granted/live in Sandbox today · ⏳ pending Sandbox tenant-admin action.

### 5.1 Teams app manifest — 16 RSC application permissions ✅

Granted per resource at install time; nothing to configure in Entra.

- **Chat/meeting scope** (consented when Alfred is added to a meeting via `+ Apps`):
  - `Calls.JoinGroupCalls.Chat` — join that chat's call (`POST /communications/calls`)
  - `Calls.AccessMedia.Chat` — app-hosted media: 16 kHz / 16-bit / mono PCM audio
  - `OnlineMeeting.ReadBasic.Chat` — meeting metadata + joinWebUrl → canonical meeting-id resolution
  - `OnlineMeetingParticipant.Read.Chat` — roster, MSI ↔ AAD ↔ display-name
  - `OnlineMeetingTranscript.Read.Chat` — official transcript GET (`useResourceSpecificConsentBasedAuthorization=true`; private meetings only)
  - `OnlineMeetingRecording.Read.Chat` — reserved
  - `ChatMessage.Read.Chat` — `GET /chats/{id}` + change-notification subscription on `chats/{id}/messages`
- **Team scope** (consented when Alfred is installed on a team):
  - `ChannelMessage.Read.Group` — subscription on `teams/{tid}/channels/{cid}/messages`
  - `ChannelMessage.Send.Group` — outbound channel sends
  - `ChannelMeeting.ReadBasic.Group` — channel-meeting discovery
  - `ChannelMeetingParticipant.Read.Group` — channel-meeting roster
  - `ChannelMeetingTranscript.Read.Group` + `ChannelMeetingRecording.Read.Group` — declared; no public GET consumes them alone (§7.2)
  - `TeamsAppInstallation.Read.Group` — install verification
  - `TeamSettings.Read.Group` + `ChannelSettings.Read.Group` — team/channel display names

### 5.2 Tenant-wide Entra application permissions ⏳ (admin consent on app `207a38a4-...`; today `requiredResourceAccess: []`)

- `Calls.JoinGroupCall.All` — join **any** meeting: arbitrary joinUrl, invites to the bot's service account, and **channel-meeting auto-join** (calls RSC exists only at chat scope — there is no team-scoped calls RSC, so channel audio is impossible without this)
- `Calls.AccessMedia.All` — app-hosted media on all such joins (required with any `Calls.*` join for media bots)
- `Calls.JoinGroupCallAsGuest.All` — guest-mode join when meeting options block directory-privileged apps
- `OnlineMeetings.Read.All` — resolve any meeting by joinUrl/id outside RSC scope
- `OnlineMeetingTranscript.Read.All` — tenant-wide official transcripts (incl. channel meetings; beta) + `getAllTranscripts` change-notification subscriptions (push replaces today's polling)
- `OnlineMeetingRecording.Read.All` — recordings + `getAllRecordings` subscriptions
- `User.ReadBasic.All` — AAD object id → `mail` / `userPrincipalName` (email-based client routing, PLAN.md; basic profile includes both fields)
- `Mail.Read` — read meeting-invite emails in Alfred's service mailbox (email → joinUrl → auto-join) + subscription on `users/{id}/messages`
- `Calendars.Read` — read the service mailbox calendar for accepted invites / joinUrl

### 5.3 Tenant/admin configuration ⏳ (policies, not Entra consent)

- Azure Bot `MsTeamsChannel`: `enableCalling: true`, `incomingCallRoute: "graphPma"`, calling webhook (§7.1 Gate 1) ✅
- Bot **service account**: Entra user + Exchange Online mailbox that receives invites; doubles as the compliance-recording application-instance UPN
- **Exchange RBAC for Applications** to scope `Mail.Read`/`Calendars.Read` to only that mailbox (management role assignment + management scope). Application Access Policies are legacy — do not create new ones.
- `New-CsApplicationAccessPolicy` + `Grant-CsApplicationAccessPolicy` to organizer accounts — prerequisite for the `OnlineMeetings.Read.All` / transcript application APIs
- **Auto-invite** (`policy_auto_invite`): `New-CsOnlineApplicationInstance` → `Sync-CsOnlineApplicationInstance` → `New/Set-CsTeamsComplianceRecordingPolicy` → `Grant-CsTeamsComplianceRecordingPolicy` (full flow: [`docs/TEAMS-AUTO-INVITE-SETUP.md`](docs/TEAMS-AUTO-INVITE-SETUP.md))
- `CsTeamsMeetingPolicy` must allow the requested join mode (§7.1)
- If `7504 Insufficient enterprise tenant permissions` persists after all of the above: Microsoft Cloud Communications support case for app-hosted-media tenant enablement

### 5.4 Compliance + operational gates

- Before persisting any media: `POST /communications/calls/{id}/updateRecordingStatus` must succeed first, and recording must be ended before reporting it stopped (Media Access API terms)
- Chat sends ride Bot Framework (`/api/messages` conversation references), not Graph — there is no application-permission Graph route for posting chat
- **Adding a new RSC requires a manifest version bump + re-install on every team/chat** — RSC binds at install time

---

## 6. Deploy

```bash
# After local edits:
git push private main
git push disney main:alfred-agent-updates --force-with-lease

# Bot VM (rebuild C# + restart service via az vm run-command).
# Full example in AGENTS.md §3.4 — canonical fields: commit SHA,
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

**Sink persistent storage.** The sink connects to `pg-alfred-disney` (Azure Database for PostgreSQL Flexible Server) via the `ALFRED_DB_URL` env var, sourced from the `alfred-db-url` Container App secret. Connection string is `postgresql://alfredadmin:****@pg-alfred-disney.postgres.database.azure.com:5432/alfred?sslmode=require`. The firewall rule `AllowAllAzureServicesAndResourcesWithinAzureIps` lets ACA reach the server. The previous sqlite-on-Azure-Files mount (`stalfreddisney/alfred-sink-data` → `/var/lib/alfred`) is no longer used — sqlite needs `fcntl` advisory locks that SMB does not provide, so every revision crashed with `database is locked` at startup and ACA silently rolled traffic back to the last-healthy revision. Don't retry that approach (see AGENTS.md §7).

```bash
az postgres flexible-server show --subscription e02c0038-82c8-4655-9647-38083f301099 \
  -g rg-alfred-disney -n pg-alfred-disney \
  --query '{state:state, version:version, sku:sku.name, fqdn:fullyQualifiedDomainName}' -o json
az containerapp secret list --subscription e02c0038-82c8-4655-9647-38083f301099 \
  -n ca-alfred-api -g rg-alfred-disney --query "[?name=='alfred-db-url'].name" -o tsv
```

**Gotchas (full list in AGENTS.md §7):**
- Always `rm -rf src/bin src/obj` before `dotnet publish`. MSBuild's incremental cache ships new-timestamped DLLs with old content.
- Use a unique `--run-command-name` per VM deploy attempt. Names are cached; rerunning the same name returns the prior attempt's output.
- VM caps at **25 managed Run Commands**. Prune Succeeded ones before deploying or `create` silently fails with `BadRequest` and then appears as `ResourceNotFound`.
- The bot's live config is `C:/teams-bot-poc/src/Config/appsettings.production.json` (not `appsettings.json`). `reloadOnChange:true` is set, so config-only changes (e.g. flipping `BlobArchive:V1CompatEnabled`) don't require a redeploy — edit the file via Run Command.
- The VM's git remote is `origin`, not `private`; the deploy script falls back through `private` → `origin`. Pushes to `private/main` reach the VM because `origin` mirrors from it.
- `IConversationReferenceStore` is file-backed at `C:/teams-bot-poc/state/conversation-references.json` so `/api/send-chat` keeps working across restarts. If the file is missing/empty (fresh bootstrap or wiped state dir), the endpoint 404s for that thread until a fresh chat activity re-populates the reference.

Before a schema change downstream consumers depend on, see §7.5 (V1 compat dual-write) and §7.6 (rollback).

---

## 7. Debug

### 7.1 Call-join failures (most common, most-frustrating)

Three authorization boundaries. Do not collapse them into "wait for `CsApplicationAccessPolicy` propagation".

**Gate 1 — Bot channel calling config (in the Azure Bot resource).** `MsTeamsChannel.incomingCallRoute` must be `graphPma`. With it `null`, the Teams calling backbone never registers the bot for the Graph PMA route.

```bash
az rest --method PATCH \
  --uri "https://management.azure.com/subscriptions/e02c0038-82c8-4655-9647-38083f301099/resourceGroups/rg-alfred-disney/providers/Microsoft.BotService/botServices/bot-alfred-disney/channels/MsTeamsChannel?api-version=2022-09-15" \
  --body '{"location":"global","properties":{"channelName":"MsTeamsChannel","properties":{"enableCalling":true,"incomingCallRoute":"graphPma","callingWebhook":"https://alfred-disney-bot.eastus.cloudapp.azure.com/api/calling","deploymentEnvironment":"CommercialDeployment","isEnabled":true,"isTeamsIvrEnabled":false,"acceptedTerms":false}}}'
```

**Gate 2 — Permission model for this specific meeting.** The manifest's `Calls.JoinGroupCalls.Chat` + `Calls.AccessMedia.Chat` are RSC scopes; they only authorize calls in chats/meetings where Alfred is installed. They do not authorize "join any meeting URL". The Sandbox app registration has `requiredResourceAccess: []`, so no tenant-wide Graph `Calls.*` roles exist on the app. Arbitrary meeting URLs require a Sandbox admin to add and admin-consent `Calls.JoinGroupCall.All` + `Calls.AccessMedia.All`.

Verify the current Sandbox app registration:

```bash
az account set --subscription 38387f0b-9a6f-46e2-8373-67422f8c2cb0
az ad app show --id 207a38a4-67c5-4ef9-ada8-ea7998734d59 \
  --query '{signInAudience:signInAudience,requiredResourceAccess:requiredResourceAccess}' -o json
az account set --subscription e02c0038-82c8-4655-9647-38083f301099
```

**Gate 3 — Sandbox tenant Cloud Communications media eligibility.** If Gate 1 is correct and Graph still returns `403 Insufficient enterprise tenant permissions`, the block is at the Microsoft Graph Communications / Teams media authorization layer before Alfred's media socket. Microsoft 2026 guidance for the 7504 payload says app-hosted media bots may require tenant enablement through Microsoft support, even when app permissions, calling config, manifest, and policies appear correct.

`CsApplicationAccessPolicy` still matters for some online-meeting application-permission APIs, but Microsoft's current policy doc lists OnlineMeetings / artifact / transcript / recording / virtual-event — not `Calls.*`. Treat it as a prerequisite to verify, not the full explanation for 7504.

**SDK state.** The bot project (under `src/`) is on `Microsoft.Graph.Communications.* 1.2.0.15690` (released 2025-10-24). NuGet also lists `1.2.0-beta.16019` (2026-02-25). Microsoft support may ask for a beta-package validation because the app-hosted-media guidance recommends staying on the newest media library or one less than three months old.

| Error / code | Cause | What to do |
|---|---|---|
| `502 CALL_JOIN_FAILED_7504_OR_7505` with `Insufficient enterprise tenant permissions` | Graph rejected `/communications/calls` before media. App has no tenant-wide `Calls.*` roles; arbitrary URL joins are outside RSC; Sandbox may need Cloud Communications enablement. | For RSC-only tests, add Alfred via `+ Apps` to that meeting/chat. For arbitrary URLs, Sandbox admin adds/admin-consents `Calls.JoinGroupCall.All` + `Calls.AccessMedia.All`, verifies policy, and opens a Microsoft Cloud Communications support case if 7504 persists. Include scenario id and timestamp. |
| `403 GRAPH_PERMISSION_MISSING` | RSC scope: manifest is older than the one with the needed scope, OR team hasn't been re-installed since a new RSC was added. | Teams Admin Center → Manage apps → Alfred Sandbox. Update on every team install. Confirm with `curl $BOT/api/channels`. |
| `403 TENANT_NOT_ENABLED_FOR_MODE` | `CsTeamsMeetingPolicy` blocks the requested join mode. | Sandbox admin allows the mode. Workaround: `policy_auto_invite` if the bot is on the meeting invite. |
| `400 BOT_NOT_INVITED` | C# join workflow's `BotAttendeePresent=true` assertion failed. | Set `BotAttendeePresent=false` (rely on Graph join), or invite the bot's service account. |
| `Audio socket up`, `PeakSample=0`, no transcript | Bot joined cleanly; buffer is silence. Speaker muted client-side or meeting options suppress app audio. | Confirm Alfred in the roster; have a human unmute and speak. Not a code bug. |

### 7.2 Channel meeting transcripts never land

Microsoft documents `OnlineMeetingTranscript.Read.Chat` as applying only to scheduled private chat meetings, not channel meetings. There is no public GET endpoint consuming `ChannelMeetingTranscript.Read.Group` alone. Workaround: schedule as a private meeting, add Alfred via `+ Apps`, then post `@Alfred link to <channel-name>` in the chat. Every event then rolls up under the named channel.

### 7.3 General health probes

```bash
BOT=https://alfred-disney-bot.eastus.cloudapp.azure.com
SINK=https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io
WEB=https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io
SA=https://stalfreddisney.blob.core.windows.net/alfred-events

curl -sS $BOT/api/calling/health | jq            # bot media readiness
curl -sS $SINK/health                            # sink
curl -sS $BOT/api/channels | jq                  # channel attachments + last_auto_join_attempt
curl -sS $SINK/v2/index | jq                     # what the sink knows about
curl -sS "$WEB/sink/v2/meetings?limit=5" | jq    # what the web homepage sees

# Manual transcript backfill. meeting_id (chat thread id of the meeting,
# 19:meeting_<base64>@thread.v2) or ephemeral call_id. organizer_oid required.
curl -sS -X POST "$BOT/api/debug/fetch-transcript" -H 'Content-Type: application/json' \
  -d '{"meeting_id":"19:meeting_NmFkYWM1NDQ...@thread.v2","organizer_oid":"...","meeting_chat_thread_id":"..."}'
```

The meeting picker at `/` polls `$WEB/sink/v2/meetings?limit=100` every
2 seconds and sorts by recent activity (`last_event_utc`), then start
metadata. During a fresh live call, Teams may not have populated
`actual_start_utc` or subject yet; the row should still appear near the
top as "Meeting on <date>" and show `live` while events are arriving.

See `AGENTS.md` §7 for the full symptom→fix index.

### 7.4 Consumer routing — bootstrap fallback and isolation

The event fanout dispatcher (under `src/Services/`) is the C# bot's HTTP delivery rail. For every non-throttled envelope, the bot independently writes the blob archive and POSTs the same envelope to each matching consumer URL. The URL path is not hard-coded by the bot; examples use `/v2/events` because both the Python sink and `server_v2.py` expose that route.

**Quick start — receive a channel's events.** On the web UI's `/channels` page (or via the API below): find the channel row → *Add consumer* → any name → your sink URL **including the path** (the bot POSTs to the URL exactly as written, e.g. `https://your-host/v2/events`) → *Save list*. Done — every matching event for that channel now POSTs to you. Semantics: every **enabled** row receives events (N rows = parallel delivery to all of them); an **empty** list falls back to `EventDispatch.BootstrapConsumerUrl`; a single **disabled** row suppresses push entirely (the isolation trick below). For meetings keyed to a person instead of a channel, use a client route (below).

The dispatcher chooses destinations in this order:

1. `meeting.*` event bound to an **email-based client route** (`meeting_routes`, see below) -> that client's sink URL (and storage-container mirror when registered). Wins outright.
2. `channel.*` event -> consumers on that `(team_id, channel_id)` attachment.
3. `meeting.*` event with `MeetingRef.channel_link` -> consumers on the linked channel attachment.
4. `meeting.*` event whose `meeting_chat_thread_id` matches an attachment's `conversation_thread_id` -> that attachment's consumers.
5. No matching attachment -> `EventDispatch.BootstrapConsumerUrl`.

That means **adding Alfred to a meeting gives the bot permission to capture that meeting; it does not tell the bot where Michael's agent lives.** If Michael adds Alfred to a private meeting and no channel link or consumer registration exists, events/transcripts still go to blob storage and the bootstrap consumer, which is our Python sink in this deployment. To send live events to Michael's `server_v2.py`, register his public endpoint as a consumer on the relevant channel, link the meeting to that channel, or run a dedicated bot instance whose `EventDispatch.BootstrapConsumerUrl` points at his endpoint.

The dispatcher has a bootstrap fallback consumer (`BotConfiguration.BootstrapConsumerUrl`) that fires whenever a channel's per-channel consumer list is empty or no channel attachment matches a meeting. Per-channel consumers win when present; otherwise the bootstrap URL fires.

The bootstrap URL is wired to the sink in this deployment. **Deleting a channel's consumer registration does NOT silence the sink** — the fallback delivers to the same URL. The `bootstrap-default` consumer at `GET /api/channels/{tid}/{cid}/consumers` is the bot auto-recording the fallback target; deleting it just routes through the real fallback in code.

To truly isolate a sink, register a placeholder consumer with `enabled: false`. `Count > 0` suppresses the fallback; `enabled: false` suppresses the placeholder itself. Net: zero POSTs.

```bash
TEAM=d3f5f412-2abf-4300-ac73-019e892c2a05
CHAN_ENC=$(printf %s "19:abc@thread.tacv2" | jq -sRr @uri)
curl -X POST "$BOT/api/channels/$TEAM/$CHAN_ENC/consumers" \
  -H "Content-Type: application/json" \
  -d '{"name":"isolation-placeholder","url":"https://disabled.invalid/events",
       "event_kinds":["*"],"enabled":false}'

# Restore — delete the placeholder, fallback resumes:
curl -X DELETE "$BOT/api/channels/$TEAM/$CHAN_ENC/consumers/isolation-placeholder"
```

A pull-based consumer that polls the blob archive directly is unaffected by either path — the bot writes blobs unconditionally, independent of consumer registration. Use the placeholder trick to silence push-based sinks; pull-based ones keep working.

Register Michael's live sidecar for a channel:

```bash
MICHAEL_URL="https://michael-agent.example.com/v2/events"
curl -X POST "$BOT/api/channels/$TEAM/$CHAN_ENC/consumers" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg url "$MICHAEL_URL" '{
        name:"michael-server-v2",
        url:$url,
        event_kinds:["meeting.transcript.final","meeting.chat.created","channel.message.created"],
        enabled:true
      }')"
```

**Email-based client routes (the no-Teams-ids path).** A client registers their email + sink URL (+ optional client-owned storage container) once — via the web UI at `/clients` or the operator API. When that person adds Alfred to a meeting, organizes one, or speaks first in its chat, the bot resolves their email (Bot Framework `TeamsInfo` first — works RSC-only; alias cache; Graph `User.ReadBasic.All` fallback) and persists a sticky `meeting_routes` binding. Every subsequent event for that meeting goes to their sink, and — when `storage_container_url` is set — the envelope is also mirrored into their container at the same canonical blob paths (§7.7). Fail-open: any resolution miss logs `client_route_unresolved` / `client_route_missing` and the event stays on the normal fallback path. State persists in `C:/teams-bot-poc/state/client-routes.json`.

```bash
# Register / update (upsert on email; storage_container_url + headers optional)
curl -X POST "$BOT/api/client-routes" -H "Content-Type: application/json" \
  -d '{"email":"michael.barron@disney.com",
       "sink_url":"https://michael-agent.example.com/v2/events",
       "storage_container_url":"https://stmichael.blob.core.windows.net/alfred-events?sv=...&sig=...",
       "event_kinds":["*"],"enabled":true}'

curl -sS "$BOT/api/client-routes" | jq                          # list
curl -sS "$BOT/api/client-routes/michael.barron%40disney.com/meetings" | jq  # bound meetings
curl -X DELETE "$BOT/api/client-routes/michael.barron%40disney.com"          # remove
```

For a private meeting that is not tied to a channel: register a client route for the person (above), link the meeting to a registered channel with `@Alfred link to <channel-name>`, or rely on the bootstrap fallback consumer.

### 7.5 V1 compatibility dual-write

A polling consumer written against the pre-v2 layout (`channels/{team}/{cid_sanitized}/chat.message/{ts}-{eid}.txt`, with the human header + `---ENVELOPE---` separator + flat `payload.sender_display_name`) breaks when the bot writes only v2.

The bot's blob archive (under `src/Services/`) has a scoped dual-write to bridge that for specific channels. When the feature flag is on and an envelope's channel id is in the allow-list, the bot writes the canonical v2 blob **and** a v1-format blob at the legacy path for the same event.

Config lives in `appsettings.production.json` (`reloadOnChange:true`):

```json
"BlobArchive": {
  "V1CompatEnabled": true,
  "V1CompatChannelIds": [
    "19:abc@thread.tacv2"
  ]
}
```

- `V1CompatEnabled` (bool, default `false`) — master switch.
- `V1CompatChannelIds` (list, default `[]`) — exact channel ids that get the extra write. Empty = zero overhead.
- Only `channel.message.{created,updated,deleted}` get compat-written. Transcripts, meeting events, etc. stay v2-only.

Lookup is O(1) (HashSet); v2 write is unchanged — compat is additive. When the downstream consumer migrates to v2, set `V1CompatEnabled: false` and the extra write goes away.

**Sidecar bridges at the repo root.** `server.py` / `server_1.py` are legacy untracked v1 polling-bridge sidecars. Leave them in place for local comparison only. `server_v2.py` is the current non-breaking v2 sidecar: it keeps the existing `/chat` API, adds `POST /v2/events` for live bot fanout, and polls the canonical archive paths (`teams/{team_sanitized}/channels/{channel_sanitized}/messages/`, optional `meetings/{meeting_sanitized}/messages/` and `meetings/{meeting_sanitized}/live_transcript/`, plus the legacy compat path while dual-write is enabled).

For `server_v2.py`, configure the Teams bridge with the same `bot_url`, `team`, `channel`, and `poll` values as before, plus a storage target. Preferred storage config is `storage_bucket` or `storage_container` with `storage_account_url`; a full container URL in `storage_bucket` also works. Legacy `blob_base` still works for existing local configs. Default `event_kinds` responds only to `channel.message.created` and `meeting.chat.created`; opt in to live transcript turns explicitly with `event_kinds=["meeting.transcript.final"]`. Runtime logs go to `/tmp/alfred-bridge-v2.log`; set `ALFRED_BRIDGE_DEBUG=1` for per-poll diagnostics.

**Consuming a client route with `server_v2.py` (both rails, no code changes):**
- **Live push** — register the client route's `sink_url` as `https://<your-host>/v2/events`; the bridge's existing `POST /v2/events` queues every envelope the bot routes to you. Add a shared secret via the route's `headers` if you want, but the bridge doesn't require one.
- **Pull from your mirror container** — point the bridge's `storage_bucket` at your `storage_container_url` value (full container URL incl. SAS query — the bridge appends the SAS to list + download calls), and set `extra_prefixes: ["meetings/"]`. Your mirror holds only your own meetings, so that one prefix covers everything; `team`/`channel` config is unnecessary for mirror-only consumption. Remember the `event_kinds` opt-in if you want live transcript turns.

Pair with §7.4's consumer-isolation trick if you also need to prevent your sink's AlfredAnalyzer from posting into the same channel while the pre-v2 consumer is the active responder.

### 7.5.1 Intent Alignment prototype consumer

`python/intent.py` is a lightweight variant of the sink focused on Intent Alignment detection rather than the full Alfred dossier. It consumes the same `alfred-v2` `POST /v2/events` contract, but the real-time loop is built around live Azure Speech `meeting.transcript.final` finalized utterance segments and live chat. It searches immutable sample source documents plus persisted JSONL memories, reflects after a short speech/chat quiet window, emits an alignment readout, and persists memory-worthy statements. `meeting.transcript.final` is not every raw audio frame or interim hypothesis; `meeting.transcript.partial` is the interim/progress event and is throttled before consumer fanout by default. The bot currently uses Azure Speech time-based segmentation with `Speech_SegmentationSilenceTimeoutMs=3000` and `Speech_SegmentationMaximumTimeMs=20000`, so live final segments emit after a 3s pause or a 20s uninterrupted speech cap. `meeting.transcript.official` is post-meeting Graph output and is intentionally not used for mid-conversation awareness.

Run locally:
```bash
cd python
INTENT_DATA_DIR=/tmp/alfred-intent uv run uvicorn intent:app --host 0.0.0.0 --port 8765
```

Live Disney sandbox endpoint:
```text
https://ca-alfred-intent.gentlewater-5aa74a73.eastus.azurecontainerapps.io/v2/events
```

Live monitor UI:
```text
https://ca-alfred-intent.gentlewater-5aa74a73.eastus.azurecontainerapps.io/ui
```

Container:
```bash
docker build -t alfred-intent -f python/Dockerfile.intent python/
docker run --rm -p 8765:8765 -v "$PWD/.intent-data:/data" alfred-intent
```

Useful endpoints:
- `GET /ui` and `/` — browser monitor for streamed live speech/chat activity, agent status lines, pending observations, rolling context, recent analyses, memories, and source overview.
- `GET /state` — JSON snapshot used by the monitor; includes pending observations plus the per-conversation rolling context buffer and total retained context count.
- `GET /stream` — Server-Sent Events stream; emits an initial `snapshot` event and live `activity` events for observations, agent search status, ignored events, and analyses.
- `POST /v2/events` / `/events` — queue live speech/chat observations.
- `POST /reflect/flush` — force pending observations to reflect immediately; useful for tests and demos.
- `POST /analyze` — manual text analysis: `{"text":"We decided to keep Postgres"}`.
- `GET /prompt` — operator-readable mechanical controls and speak-only policy; timing is enforced by Azure Speech segmentation and sink timers before analysis runs.
- `GET /sources` — rough source/index overview.
- `GET /search?q=postgres%20sqlite` — inspect source + memory hits.
- `GET /memories` and `POST /memories` — inspect or seed persisted memories.
- `GET /analyses` — recent alignment outputs.

Runtime knobs:
- `INTENT_SPEECH_REFLECT_SECONDS` — quiet-window after a live speech final arrives before reflection; default `1`.
- `INTENT_CHAT_REFLECT_SECONDS` — chat burst quiet-window before reflection; default `1`.
- `INTENT_MAX_REFLECT_BATCH` — max observations before immediate reflection; default `12`.
- `INTENT_ROLLING_BUFFER_SIZE` — number of recent observations shown per conversation in `/state` and `/ui`; default `60`. Full in-process conversation context is still retained for reflection.
- `INTENT_SEND_CHAT_URL` or `BOT_SEND_CHAT_URL` — optional `$BOT/api/send-chat` endpoint for real chat responses. When configured, intent responses use the canonical `send_to_meeting_chat` tool path; without it, the tool records a dry-run just like the main agent.

Register it like any other consumer:
```bash
curl -X POST "$BOT/api/channels/$TEAM/$CHAN_ENC/consumers" \
  -H "Content-Type: application/json" \
  -d '{"name":"intent-demo","url":"https://<host>/v2/events",
       "event_kinds":["meeting.transcript.final","meeting.chat.created","channel.message.created"],
       "enabled":true}'
```

### 7.6 Rollback after a bad deploy

Every full-stack deploy pushes `deployed-{bot,sink,web}-YYYY-MM-DD` tags to the `private` remote pinning the SHAs that were live before cutover. ACR retention keeps prior images (`disney-sandbox-{sha}`). Rollback is one Run Command per component:

```bash
# Bot
az vm run-command create --subscription <sub> \
  --vm-name vm-alfred-disney -g rg-alfred-disney \
  --run-command-name alfred-rollback-$(date +%s) --location eastus \
  --script 'Set-Location C:/teams-bot-poc; git fetch origin --tags; git reset --hard deployed-bot-2026-05-13; Stop-Service TeamsMediaBot -Force; rm -r src/bin,src/obj -ErrorAction SilentlyContinue; dotnet publish src --configuration Release --output src/bin/Release/net8.0/publish; Start-Service TeamsMediaBot'

# Sink / Web — point Container App back to the old image tag
az containerapp update -n ca-alfred-api -g rg-alfred-disney --image acralfreddisneye02c0038.azurecr.io/ca-alfred-api:disney-sandbox-0baf2af
az containerapp update -n ca-alfred-web -g rg-alfred-disney --image acralfreddisneye02c0038.azurecr.io/ca-alfred-web:disney-sandbox-4026981
```

### 7.7 Consuming captured data

Everything Alfred captures is delivered through two sibling rails from the C# bot. Both use the same `alfred-v2` envelopes.

| Path | Best for |
|------|----------|
| **Sink API** — `$SINK/v2/*` | "One HTTP call → JSON" — our reference consumer's PostgreSQL view |
| **Blob archive** — `$SA/...` | "Raw event stream forever" — replay, bulk, offline |

Full contract + recipes: [`docs/retrieving-transcripts.md`](docs/retrieving-transcripts.md).

```bash
SINK=https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io
SA=https://stalfreddisney.blob.core.windows.net/alfred-events

# Meetings in the system
curl -sS "$SINK/v2/meetings?limit=20" | jq '.meetings[] | {meeting_id, subject, scheduled_start_utc}'

# Subject → meeting_id (case-insensitive substring)
curl -sS "$SINK/v2/resolve?kind=meeting&subject=sprint%20planning" | jq

# Official Microsoft transcript (plaintext, inline)
MID="19:meeting_NmFkYWM1NDQ...@thread.v2"
MID_SAN=$(printf %s "$MID" | sed -E 's/[^a-zA-Z0-9_.-]/_/g' | cut -c1-200)
curl -sS "$SINK/v2/meetings/$MID/transcript" | jq -r '.text'
# Or raw blobs:
curl -sS "$SA/meetings/$MID_SAN/transcripts/official.txt"
curl -sS "$SA/meetings/$MID_SAN/transcripts/official.vtt"

# Full ledger (live STT + chat)
curl -sS "$SINK/v2/meetings/$MID/events?limit=500" | jq

# Channel messages
TID="d3f5f412-..." CID="19:abc@thread.tacv2"
curl -sS "$SINK/v2/teams/$TID/channels/$CID" | jq
curl -sS "$SINK/v2/teams/$TID/channels/$CID/events?kinds=chat&limit=200" | jq
```

**Blob path layout.** Folder segments are logical *categories*, not raw event_types — one folder per data type. `messages/` mirrors Graph's `/messages` endpoint; `transcripts/` mirrors Graph's `/transcripts` callTranscript resource. `live_transcript/` and `lifecycle/` are our own labels.

Sanitization rule: `[^a-zA-Z0-9\-_.]` replaced with `_`, max 200 chars. Worked example: `19:meeting_NmFkYWM1NDQ...@thread.v2` → `19_meeting_NmFkYWM1NDQ..._thread.v2`.

```
# Channel scope (channel meetings have no audio → messages + lifecycle only)
teams/{team_id_sanitized}/channels/{channel_id_sanitized}/messages/{utcTs}-{event_id}.json
teams/{team_id_sanitized}/channels/{channel_id_sanitized}/lifecycle/{utcTs}-{event_id}.json

# Meeting scope (meeting_id is the chat thread id, sanitized)
meetings/{meeting_id_sanitized}/messages/{utcTs}-{event_id}.json          ← meeting.chat.{created,updated,deleted}
meetings/{meeting_id_sanitized}/live_transcript/{utcTs}-{event_id}.json   ← meeting.transcript.{partial,final}  (Azure Speech STT)
meetings/{meeting_id_sanitized}/transcripts/{utcTs}-{event_id}.json       ← meeting.transcript.official envelope (Graph callTranscript)
meetings/{meeting_id_sanitized}/transcripts/official.txt                  ← flat plaintext (overwritten on each fetch)
meetings/{meeting_id_sanitized}/transcripts/official.vtt                  ← flat WebVTT     (overwritten on each fetch)
meetings/{meeting_id_sanitized}/lifecycle/{utcTs}-{event_id}.json         ← meeting.{created,ended,linked,call.joined,call.left}
```

Real production URL example:
```
https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io/archive?prefix=meetings%2F19_meeting_NmFkYWM1NDQtYTM3ZC00ZjlmLTk4ZjItZjE0M2YwOWEzODIx_thread.v2%2Flive_transcript%2F
```

The precise `event_type` lives in the envelope JSON; consumers that need `created` vs. `updated` vs. `deleted` read it from there.

Every `.json` blob is a pure `alfred-v2` envelope — no preamble, just `{ … }`. `jq` it directly.

> **Legacy v1 compat path (preserved):** `channels/{tid}/{cid_sanitized}/chat.message/{ts}-{eid}.txt` is still written for channel ids in `BlobArchive:V1CompatChannelIds` — see §7.5. New consumers must read the v2 category layout above.

> **Channel meetings have no audio.** They surface only as `channel.message.*` events.

> **`meeting_id` is canonical.** It is the Teams chat thread id of the meeting (`19:meeting_<base64>@thread.v2`). Graph's `onlineMeeting.id` is a separate identifier the system does not key on.

---

## 8. Editing rules (do not violate)

1. **`meeting_id` is the Teams chat thread id of the meeting** (`19:meeting_<base64>@thread.v2`). Graph's `onlineMeeting.id` is a separate identifier and is not used as the key. Never substitute a different surrogate.
2. **Discriminated envelope.** Every `AlfredEventEnvelope` has exactly one of `ChannelRef` or `MeetingRef` populated, never both, never neither. Discriminator is the `event_type` prefix (`channel.*` → `ChannelRef`; `meeting.*` → `MeetingRef`).
3. **No v1 shims.** Do not add "if old schema, do X else Y" branches. `alfred-v2` is the only schema the bot emits. v1 is dead.
4. **Channel meetings have no `meeting.*` events.** They surface only as `channel.message.*` on the channel thread. Do not model channel meetings as first-class `Meeting` entities.
5. **One inbound chat path** — Bot Framework `/api/messages` → C# bot publishes via the event fanout dispatcher. No parallel ingress.
6. **Outbound Alfred chat** goes through the `send_to_meeting_chat` tool only.
7. **`alfred.yaml` is the sole source of truth** for prompt and intervention policy. The analyzer raises at construction if `instructions` is missing — do not add a code fallback.
8. **One canonical implementation per core concern.** No duplicate C# bot, sink, or UI implementations. Client-side bridge examples such as `server_v2.py` are allowed because they are consumers, not alternate core paths.
9. **Fail fast.** Clear, specific errors at system boundaries. No validation for impossible scenarios.
10. **`dotnet publish` always after `rm -rf src/bin src/obj`** in any deploy script that touches code. See AGENTS.md §7.4.
11. **Manifest changes** require: bump `version`, regenerate `alfred-sandbox.zip`, re-import in Teams Developer Portal, re-grant admin consent if RSCs changed. **New RSCs require team re-install.**
12. **Push protocol** — only `private` and `disney` remotes. Never `origin`.

---

## 9. Pointers

- [`AGENTS.md`](AGENTS.md) — deep ops manual (build internals, debug recipes, deploy mechanics, gotchas).
- [`docs/event-contract.md`](docs/event-contract.md) — `alfred-v2` envelope schema + per-`event_type` payload.
- [`docs/retrieving-transcripts.md`](docs/retrieving-transcripts.md) — blob archive layout + Python / curl / az recipes.
- [`docs/TEAMS-AUTO-INVITE-SETUP.md`](docs/TEAMS-AUTO-INVITE-SETUP.md) — one-time Sandbox-tenant admin setup for auto-invite mode.
- [`PLAN.md`](PLAN.md) — implementation plan for email-based client routing.
- [`TODO.md`](TODO.md) — prioritized backlog with code-level paths.

---

## 10. Adding Alfred to a Teams meeting

Alfred is one Teams app with two attach surfaces.

### Surface 1 — "+Apps" in the meeting chat

Installs Alfred per-meeting in the meeting's chat container. The manifest's chat-scoped RSCs become consented for that one chat.

You get:
- Real-time chat events (`meeting.chat.created/updated/deleted`) → bot → sink → dossier.
- **Auto-fetched** post-meeting Microsoft transcript via `OfficialTranscriptFetcher`. The fetcher polls Graph's per-meeting `transcripts` endpoint (gated by the chat-scoped `OnlineMeetingTranscript.Read.Chat` RSC, evaluated via `useResourceSpecificConsentBasedAuthorization=true`) and emits `meeting.transcript.official` to the sink + `meetings/{mid}/transcripts/official.{txt,vtt}` to blob storage. Two triggers cooperate: first chat sighting (start) registers an initial 30-min poll window; `OnTeamsMeetingEndAsync` extends it to "30 min after end" no matter how long the meeting ran. One bounded retry one hour later if the first window misses. State is persisted to `C:/teams-bot-poc/state/pending-transcript-fetches.json` so a redeploy mid-poll doesn't drop the fetch. Operator backfill via `POST $BOT/api/debug/fetch-transcript` (§7.3) remains the escape hatch for misses beyond that envelope.
- Alfred can post into the chat via `/api/send-chat` (the agent's `send_to_meeting_chat` tool).

You DON'T get: live audio, `meeting.transcript.partial/final`, the bot in the meeting roster, speaker diarization for live notes.

How: meeting chat → **+** → **Apps** → search "Alfred Sandbox" → Add. Sandbox admin approval may be required if the manifest version is newer than the installed version.

### Surface 2 — Alfred as a call participant (live audio)

Bot enters the call as an actual participant via `Calls.JoinGroupCalls.Chat` + `Calls.AccessMedia.Chat` RSCs — but only for a meeting/chat where Alfred is installed. The current Sandbox app registration has no tenant-wide `Calls.*` roles, so arbitrary "join this URL" is not authorized.

You get everything from Surface 1, plus: live STT (`meeting.transcript.partial/final`), speaker diarization with AAD ids, live participant roster events, Alfred visibly in the roster, real-time interventions.

Three paths, in order of automation:

1. **Auto-join for channel meetings.** If the channel is attached (`POST $BOT/api/channels/attach`, §7.4) and `auto_join_enabled: true`, the bot joins when Teams posts a `callStartedEventMessageDetail`.
2. **Manual join.** `POST $BOT/api/calling/join` with the meeting's `joinUrl`. Surfaces 502/7504/7505 if outside Alfred's RSC grant or Sandbox isn't enabled for app-hosted media (§7.1).
3. **Invite the bot's service account** via the organizer's calendar. Still needs the Graph Communications media authorization in §7.1.

### Doing both

**Yes, you can (and often should) do both.** "+Apps" gives the chat surface; the call-participant path adds audio on top. Additive, not duplicative.

**No double agent responses.** Alfred is one bot with one identity. Both surfaces feed events into the SAME `AlfredAnalyzer` instance scoped to the same chat thread. The analyzer is event-driven and debounced — each tick is ONE LLM call producing AT MOST one `send_to_meeting_chat`. The session merge logic is idempotent on `id`: if the same statement appears in chat and audio, both flow through the SAME action_item id and the second observation just updates it.

What changes when you add both: more data per tick → more ticks; richer dossier (direct quotes, speaker attribution); silence-default still applies to chat output.

When to pick one:
- **"+Apps" only** for post-meeting retrieval + chat presence where live agent posts would be intrusive, or when the calling policy gate isn't open.
- **Call-only (no +Apps)** is unusual — chat events come via the chat-resource subscription which requires the `+Apps` install. You'd get audio-only Alfred, losing chat context.

For the common case (Disney Sandbox internal meetings): do BOTH. `+Apps` first, then once the calling policy is open let auto-join handle audio, or hit the manual join endpoint.
