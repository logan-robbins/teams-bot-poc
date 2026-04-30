# Alfred

Microsoft Teams meeting assistant for The Walt Disney Company. Joins a
Teams meeting, captures audio, transcribes per-speaker, ingests meeting
chat, runs an LLM agent that produces a live "dossier" (decisions, open
questions, action items, risks), and posts back into the meeting chat.

This is the only README. Read it end-to-end before touching code.

---

## 1. Why this app needs the permissions it asks for

When the Alfred app is installed into a Teams meeting / chat, Teams
prompts the installer to grant **five chat-scoped, resource-specific
consent (RSC) permissions**. They are scoped to the single chat the app
is added to — Alfred has no tenant-wide Graph access. The live POC uses
Bot Framework meeting-chat activities for chat ingress; the chat RSC
permissions remain in the manifest for the Graph notification workstream
documented in `PROD.md`.

| Permission | What it lets Alfred do | Why it is required |
|---|---|---|
| `Calls.JoinGroupCalls.Chat` (Application) | Join the Teams call attached to this chat as a bot. | Without this, Alfred cannot enter the meeting at all. The bot would be installed but never appear as a participant. |
| `Calls.AccessMedia.Chat` (Application) | Receive the live unmixed audio stream from the meeting (16 kHz / 16-bit / mono PCM). | Audio capture is the entire transcription path. Without it, every transcript event is empty and the dossier never gets built. Microsoft also requires the bot's AppId to be on the **Real-Time Media (RTM) allowlist** before media actually attaches even after consent — request via `https://aka.ms/teams-rtm-onboarding` (~2 weeks turnaround). |
| `OnlineMeetingParticipant.Read.Chat` (Application) | Read the participant roster of this meeting: AAD object id, display name, MSI (`MediaSourceId`) per audio media stream. | This is the source of truth for **who is speaking**. The Graph Communications SDK exposes `ICall.Participants.MediaStreams[].SourceId` bound to `Info.Identity.User.Id`. Without it, the agent only sees `speaker_0` / `speaker_1` from STT diarization and cannot attribute decisions/actions to a real person. |
| `ChatMessage.Read.Chat` (Application) | Enables the planned Microsoft Graph change-notification path for meeting chat. | Current live chat ingress is Bot Framework `/api/messages`; this permission is kept so the install consent prompt is stable when the Graph path in `PROD.md` is implemented. |
| `ChatMessageReadReceipt.Read.Chat` (Application) | Observe read-receipts on chat messages. | Lightweight planned signal that lets Alfred avoid re-asking questions everyone has already seen and "engaged with". Kept in the manifest so the install consent prompt is one-shot and stable. |

The app also requests two **manifest-level Teams app permissions**
(`identity`, `messageTeamMembers`) — these are the standard set required
for any bot that posts into a chat under its own identity. They are not
Graph permissions.

**Critically: there are zero tenant-wide Graph permissions.** All five
RSC permissions are granted at install time, scoped to the single chat
or meeting the installer adds Alfred to. A different chat = a separate
consent.

If your tenant policy treats the Alfred app as an "unverified publisher"
and gates user-driven consent, an M365 Global Administrator must grant
org-wide consent in **Teams Admin Center → Manage apps → Permissions**.
The manifest cannot bypass this.

---

## 2. System

```
                    ┌──────────────────────────────┐
                    │    Microsoft Teams meeting   │
                    │   (audio + chat + roster)    │
                    └──────────────┬───────────────┘
                                   │
                  unmixed audio    │   Bot Framework chat activities
                  + participants   │   (/api/messages)
                                   ▼
        ┌────────────────────────────────────────────────────┐
        │           C# bot  (src/, runs on Windows VM)       │
        │  - Graph Communications SDK joins the call         │
        │  - Receives audio + per-buffer ActiveSpeakers MSI  │
        │  - Reads ICall.Participants → MSI ↔ AAD mapping    │
        │  - Streams PCM to Deepgram / Azure ConversationTr. │
        │  - Forwards transcript + chat to the Python sink   │
        │  - Sends Alfred's outbound chat via Bot Framework  │
        └────────────────────────┬───────────────────────────┘
                                 │
              POST /transcript   │   POST /chat
              POST /session/...  │   POST /api/send-chat ◀── reverse path
                                 ▼
        ┌────────────────────────────────────────────────────┐
        │      Python sink  (python/, FastAPI on CAE)        │
        │  - Append-only meeting ledger (MeetingEvent)       │
        │  - Debounced AlfredAnalyzer → AlfredExtraction     │
        │  - send_to_meeting_chat tool (sole action surface) │
        │  - SQLite: sessions / meeting_events / extractions │
        │           / tool_calls / dossier_items             │
        │  - SSE /session/events drives the UI               │
        └────────────────────────┬───────────────────────────┘
                                 │ SSE + JSON
                                 ▼
        ┌────────────────────────────────────────────────────┐
        │          React UI  (web/, Vite + React 19)         │
        │   3 columns: Ledger | Dossier | Companion Rail     │
        │   Read-only: only Alfred speaks into chat.         │
        └────────────────────────────────────────────────────┘
```

**Agent contract (do not change without reading §6):** the analyzer
emits one `AlfredExtraction` per tick (rolling summary, topics, notes,
decisions, open questions, action items, risks — merged by `id`) and
optionally calls one tool — `send_to_meeting_chat(text, kind, ...)`.
There is no `SEND/ASK/SILENT` enum. Silence is "did not call the tool".

---

## 3. Disney environment

| Thing | Value |
|---|---|
| Azure subscription | `e02c0038-82c8-4655-9647-38083f301099` |
| Azure tenant | `56b731a8-...` (disney.com) |
| M365 tenant (Teams + Entra app) | `38387f0b-9a6f-46e2-8373-67422f8c2cb0` (plutosdoghouse.com) |
| Resource group | `rg-alfred-disney` |
| Bot VM | `vm-alfred-disney` (`alfred-disney-bot.eastus.cloudapp.azure.com`) |
| Azure Bot Service | `bot-alfred-disney` (SingleTenant, app tenant = M365 tenant above) |
| Bot AppId | `207a38a4-67c5-4ef9-ada8-ea7998734d59` |
| Sink (Container App) | `https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io` |
| UI (Container App) | `https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io` |
| Azure OpenAI | `aoai-alfred-disney` (`gpt-5-mini`, GlobalStandard cap 10) |
| Speech Services | `speech-alfred-disney` (eastus, S0) |
| Teams app manifest zip | `manifest/alfred-sandbox.zip` |

**Source of truth repo:** `git@github.com:logan-robbins/alfred-teams-bot.git` (`main`). Private. Local `main` tracks `private/main`.

---

## 4. Repo layout

```
src/                         C# Teams bot (Graph Communications SDK)
python/                      FastAPI sink + Alfred agent
  transcript_sink.py         all HTTP routes
  meeting_agent/             models, session, agent, tools, persistence
  batcave_platform/          product-spec loader, output routes
    specs/alfred.yaml        Alfred's prompt + intervention policy
  variants/alfred.py         spec-bound runtime variant
  tests/                     pytest suite (uv run pytest tests -v)
web/                         React 19 + Vite + Tailwind v4 dossier UI
manifest/                    Teams app manifest (Disney sandbox)
scripts/                     deploy-azure-vm.sh, deploy-azure-agent.sh, ...
```

Canonical state object: `InterviewSession` in
`python/meeting_agent/models.py`. Canonical history:
`InterviewSession.meeting_events` (a single append-only ledger of
normalized speech + chat + system events).

---

## 5. Install in the Disney M365 tenant

1. Upload `manifest/alfred-sandbox.zip` via the Teams Developer Portal at `https://dev.teams.microsoft.com/apps` → **Import app**.
2. **Preview in Teams** → **Add to a chat** (or to a meeting). The install grants the five RSC permissions described in §1, scoped to that chat.
3. Either invite the bot into a meeting from that chat, or trigger an explicit join:
   ```bash
   ./scripts/join_meeting.sh "<teams-meeting-join-url>" "Alfred"
   ```
4. If the publisher is "unverified" by tenant policy, see the admin-consent note in §1.

---

## 6. Operate

**Live validation note (2026-04-30):** the Disney sandbox VM is configured
to POST transcripts to the sink at `/transcript` and chat events to `/chat`.
The live meeting-chat smoke test validated the current canonical POC flow:

1. Teams meeting chat message reaches the Windows VM through the Bot
   Framework `/api/messages` endpoint.
2. `AlfredBot` forwards the message to the Python sink `/chat` endpoint.
3. The sink writes it into `InterviewSession.meeting_events`.
4. The React UI reads it through `/sink/session/status` and live SSE.

Microsoft Graph chat-notification subscriptions are not active in the live
sandbox deployment. Chat ingress is the Bot Framework `/api/messages` path
(`source="bot_framework"` in the ledger). `TranscriptSink.ChatEndpoint` is
required; the bot fails fast when it is missing.

```bash
# Public health
curl -sS -m 10 https://alfred-disney-bot.eastus.cloudapp.azure.com/api/calling/health
curl -sS -m 10 https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io/health
curl -sS -m 10 -o /dev/null -w "%{http_code}\n" https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io

# Sink stats
curl -sS https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io/stats | jq

# UI-to-sink proxy. This must return the same active session state as the
# direct sink; if it 404s, redeploy the web container with web/nginx.conf.template.
curl -sS https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io/sink/session/status | jq

# Restart the bot service on the VM
az vm run-command create -g rg-alfred-disney --vm-name vm-alfred-disney --location eastus \
  --run-command-name restart-bot \
  --script 'Restart-Service TeamsMediaBot -Force; Start-Sleep -Seconds 8; Get-Service TeamsMediaBot' \
  --async-execution false --timeout-in-seconds 60
az vm run-command delete -g rg-alfred-disney --vm-name vm-alfred-disney --run-command-name restart-bot --yes

# Tail bot logs
az vm run-command create -g rg-alfred-disney --vm-name vm-alfred-disney --location eastus \
  --run-command-name tail-logs \
  --script 'Get-Content C:\teams-bot-poc\logs\service-output.log -Tail 80; Write-Host "---STDERR---"; Get-Content C:\teams-bot-poc\logs\service-error.log -Tail 40 -ErrorAction SilentlyContinue' \
  --async-execution false --timeout-in-seconds 60
az vm run-command show -g rg-alfred-disney --vm-name vm-alfred-disney --run-command-name tail-logs --instance-view --query "instanceView.output" -o tsv
az vm run-command delete -g rg-alfred-disney --vm-name vm-alfred-disney --run-command-name tail-logs --yes
```

**Always use `az vm run-command create`. Never `az vm run-command invoke`** — the legacy action variant wedges the extension and forces a manual `Microsoft.CPlat.Core.RunCommandWindows*` cleanup over RDP.

---

## 7. Push a code change

```bash
git push                                 # local main → private/main

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

# Sink + UI Container Apps (rebuild images via ACR, redeploy)
./scripts/deploy-azure-agent.sh
```

`SKIP_REPO_SYNC=0` is required after pushing new commits. Use
`SKIP_REPO_SYNC=1` for config-only redeploys.

The bot service is stopped before publishing so the running DLL doesn't
lock the build. If you ever see "file in use" during publish, the
service wasn't stopped — re-run.

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
cd python && uv run pytest tests -v       # baseline: 97 passed, 2 skipped

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
| `/api/calling/health` Healthy but Teams calls don't join | Microsoft RTM media allowlist required for the bot AppId. Submit at `https://aka.ms/teams-rtm-onboarding` (~2 weeks). Until approved, the bot joins but the media SDK fails to attach to audio. |
| Bot service Running, stderr says `MediaPlatform needs at least 2 cores` | VM has <2 physical cores. Resize: `az vm resize -g rg-alfred-disney -n vm-alfred-disney --size Standard_D4s_v3`, then restart bot service. |
| Bot service Running, stderr says `DllNotFoundException: NativeMedia` | Server-Media-Foundation feature or VC++ Redistributable missing. Re-run `./scripts/deploy-azure-vm.sh`; Phase 1 reinstalls both. |
| `POST /api/messages` → 401 `Invalid AppId passed on token` | `appsettings.production.json` is missing `MicrosoftAppId/Password/Type/TenantId` at the **root** (Bot Framework reads these, distinct from `Bot.*`). Re-run `./scripts/deploy-azure-vm.sh`. |
| Bot crashes on startup with `Certificate with thumbprint '...' not found` | The TLS cert was rotated/renewed but `appsettings.production.json` still has the old thumbprint. The bot now auto-resolves the cert by Subject CN matching `MediaPlatformSettings.ServiceFqdn` (or `CertificateFriendlyName` prefix) when the configured thumbprint is missing — restarting the service is enough. If you do see this error, the cert is missing entirely; run `./scripts/deploy-azure-vm.sh` to re-issue it. |
| `POST /api/messages` → 500 `CloudAdapter ambiguous constructors` | DI regression. The factory in `src/Program.cs` must explicitly select `CloudAdapter(BotFrameworkAuthentication, ILogger)`. |
| Sink `events_received` increments but `session_events: 0` and no `[CHAT]` log line | No active session. The sink only persists chat to SQLite when a session is active. `POST /session/start` first. |
| `az vm get-instance-view` returns `vmAgent: null` | ARM cache lag. Probe directly with a Run Command (`Write-Host alive`); if it returns Succeeded the agent is fine. |
| `Conflict: Run command extension execution is in progress` | Legacy `az vm run-command invoke` wedged the extension. RDP in, remove `C:\Packages\Plugins\Microsoft.CPlat.Core.RunCommandWindows*`, restart `WindowsAzureGuestAgent` + `RdAgent`. |
| `git fetch origin main` on VM exits 0 but `origin/main` doesn't move | Stale single-branch refspec. Run `git config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'` then `git fetch --prune origin`. The bootstrap fixes this automatically when `remote.origin.url` changes. |
| Bootstrap aborts with `REPO_URL='git@...' is SSH but DEPLOY_KEY_FILE='...' is empty` | The deploy script needs the SSH deploy key. See §10. |

---

## 10. Deploy key (private repo)

VMs clone the private repo over SSH using a read-only ed25519 deploy
key registered on the repo. Bootstrap drops it on the VM at
`C:\ProgramData\alfred\deploy_key` with ACL `SYSTEM + azureuser : Read`
and points `GIT_SSH_COMMAND` at it.

```bash
# Generate (one-time per dev machine — public half already registered)
ssh-keygen -t ed25519 -C "alfred-teams-bot deploy" -f /tmp/alfred-deploy-key -N ""
chmod 600 /tmp/alfred-deploy-key

# Register the public half on the private repo (read-only)
gh api repos/logan-robbins/alfred-teams-bot/keys \
  -f title="$(hostname)-$(date -u +%Y-%m-%d)" \
  -f key="$(cat /tmp/alfred-deploy-key.pub)" \
  -F read_only=true
```

---

## 11. Editing rules (do not violate)

1. Single canonical meeting ledger (`InterviewSession.meeting_events`).
2. One authoritative inbound chat path for the current POC: Teams Bot Framework activities delivered to `/api/messages`, then forwarded to the Python `/chat` endpoint.
3. Outbound Alfred chat goes through the `send_to_meeting_chat` agent tool. The old `teams_chat` output route is gone.
4. Agent contract = `AlfredExtraction` (structured output) + `send_to_meeting_chat` (sole action). **Do not** reintroduce a `SEND/ASK/SILENT` enum.
5. UI is read-only with respect to the meeting. Only Alfred speaks into the meeting chat, and only through the tool.
6. All persistent writes go through `meeting_agent.persistence.SessionStore` so live UI and post-meeting replay read the same truth.
7. Treat this README and `python/batcave_platform/specs/alfred.yaml` as system documents.

---

## 12. What's next (productionalization)

`PROD.md` at the repo root is the next-step productionalization plan. It
covers five technical enhancements layered on the current POC: an
append-only raw ingest audit store, a split between raw audit and the
agent's working memory, a Teams-MSI-driven participant identity layer
(so the agent sees real names instead of `speaker_0`), explicit
proactivity rules in `alfred.yaml` (without reintroducing a
`SEND/ASK/SILENT` enum), and per-meeting sink routing so each call can
target its own Python sink. Read `PROD.md` before starting any of those
workstreams.
