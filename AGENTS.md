# AGENTS.md — Operator manual for AI coding agents

This is the runbook for an AI coding agent dropped into this repo. It
captures everything you need to be productive on day zero: how to read
the architecture, how to build and deploy, how to debug the hard parts,
and what *not* to do. Read `README.md` first for product context;
this file is the operational layer underneath it.

---

## 1. The 60-second mental model

Alfred is a Microsoft Teams meeting assistant. Three deployables:

```
Teams ──► C# bot (Windows VM, Graph Communications SDK + Bot Framework)
               │
               │ POST /transcript    POST /chat   POST /session/link
               ▼
         Python sink (FastAPI on Azure Container Apps)
               │
               │ SSE + JSON
               ▼
         React UI (Vite + React 19, Container App)
```

- The **C# bot** lives on a Windows VM (`vm-alfred-disney`) because the
  Graph Communications Media SDK that captures Teams audio is
  Windows-only and needs the Server-Media-Foundation feature.
- The **Python sink** owns the per-meeting session state, the
  intervention policy, the dossier, and the SQLite ledger. It runs as
  a Container App because it doesn't need Windows or media.
- The **React UI** is read-only with respect to the meeting; it just
  visualizes the sink's state via SSE.

The single key that ties everything together is `chat_thread_id`
(`19:meeting_xxx@thread.v2` for a meeting, `19:{channelId}@thread.tacv2`
for a Teams channel). Every transcript, chat, raw-audit row, and SSE
event is keyed on it. The UI URL is `/m/<chat_thread_id>`.

For analytics across an entire channel (chat + every meeting + every
transcript), every event also carries optional `team_id` /
`channel_id` / `channel_thread_id` so you can `WHERE channel_id = ?`
without joins.

---

## 2. Repo layout — where the code lives

| Path | Owner | Build | Deploy target |
|---|---|---|---|
| `src/` | C# Teams bot | `dotnet publish` (.NET 8, Windows-x64) | `vm-alfred-disney` VM, runs as `TeamsMediaBot` Windows service via NSSM |
| `python/` | FastAPI sink + Alfred agent | `uv sync` / `uv run` | `ca-alfred-api` Container App |
| `web/` | React 19 + Vite UI | `npm install && npm run build` | `ca-alfred-web` Container App |
| `manifest/` | Teams app manifest (`manifest.json`, `alfred-sandbox.zip`) | `cd manifest && zip -j alfred-sandbox.zip manifest.json color.png outline.png` | Imported via Teams Developer Portal |
| `scripts/` | deploy + ops scripts | n/a | n/a |
| `python/meeting_agent/` | Canonical session/agent state | — | — |
| `python/batcave_platform/` | Product spec loader. `specs/alfred.yaml` is the **only** source of truth for Alfred's prompt + intervention policy. | — | — |

Canonical types:
- **Session state**: `python/meeting_agent/models.py` `InterviewSession`
- **Persistent ledger**: SQLite at `python/output/alfred-disney-sandbox-prod/alfred.sqlite3`,
  schema in `python/meeting_agent/persistence.py`
- **Sole outbound action**: `send_to_meeting_chat` tool in
  `python/meeting_agent/tools.py`. Do not introduce a parallel
  outbound path.

---

## 3. Environment setup (macOS / Apple Silicon dev box)

### 3.1 Required tools

```bash
# Verify everything is present.
brew list | grep -E '^(uv|jq|gh|node|docker)$'
# uv      → Python tooling
# jq      → JSON munging
# gh      → GitHub CLI (used for some auth flows)
# node    → web/ build (Vite/React)
# docker  → C# build verification (we don't install dotnet locally — see §3.3)
```

Azure CLI:
```bash
az --version | head -3        # 2.50+
az login                      # WDI R&D subscription
az account set --subscription e02c0038-82c8-4655-9647-38083f301099
```

### 3.2 Python (`uv` only — never `pip`, never raw `python`)

```bash
cd python
uv sync                                # install/update deps from pyproject.toml + uv.lock
uv run pytest tests -q                 # baseline: 121 passed, 2 skipped
uv run python run_variant_sink.py --instance dev --port 8765 \
  --product-spec batcave_platform/specs/alfred.yaml
```

Anything that does `pip install`, `python script.py`, or `source .venv/bin/activate`
is **wrong** in this repo. Use `uv add <pkg>` to add dependencies; use
`uv run <cmd>` for everything else.

### 3.3 C# build — Docker, not local dotnet

There is **no** local `dotnet` on dev. C# verification is done via the
official .NET 8 SDK container with a *named NuGet volume* (a one-time
fresh restore is ~12 minutes; with the cache it's seconds):

```bash
docker volume create alfred-nuget-cache    # one-time
cd /Users/logan.robbins/research/teams-bot-poc

docker run --rm \
  -v "$(pwd):/work" \
  -v alfred-nuget-cache:/root/.nuget/packages \
  -w /work/src \
  mcr.microsoft.com/dotnet/sdk:8.0 \
  dotnet build --configuration Release --nologo /v:m
```

Without the named volume, every build does a fresh 12-minute restore
because `docker run --rm` discards the package cache. Always use the
volume.

The actual production build runs on the Windows VM (`dotnet publish`)
during deploy — this local docker build is *only* for catching
compile errors before pushing.

### 3.4 Web

```bash
cd web && npm install && npm run dev    # http://127.0.0.1:5173
```

---

## 4. Login + secrets

### 4.1 Azure CLI

```bash
az login                                              # browser flow
az account set --subscription e02c0038-82c8-4655-9647-38083f301099
az account show --query "{name:name, id:id}" -o tsv   # confirm WDI R&D
```

### 4.2 Git remotes

Three remotes, each with a different role:

| Remote | URL | Role |
|---|---|---|
| `origin` | `github.com/logan-robbins/teams-bot-poc` | Public mirror |
| `private` | `github.com/logan-robbins/alfred-teams-bot` | Source of truth for the VM (deploy key references this) |
| `disney` | `gitlab.wdi.disney.com/Michael.Barron.-ND/teams_integration` | Disney internal review; `alfred-agent-updates` branch is the active MR |

Push protocol after a feature commit:
```bash
git push origin main
git push private main
git push disney main:alfred-agent-updates --force-with-lease   # MR branch on gitlab
```

The disney remote uses an embedded PAT in the URL; treat it as opaque
(don't try to refresh it without the user). Local SSH for `private`/`origin`
uses your `~/.ssh` keys.

### 4.3 Deploy secrets (Disney sandbox)

The full deploy script `scripts/deploy-azure-vm.sh` expects these files
in `/tmp/` (it will fail fast if they're missing):

| File | Format | Where to get it |
|---|---|---|
| `/tmp/alfred-deploy-key` | SSH private key | The public half is registered on the GitHub repo as a deploy key. Generate locally + register; or copy from secure storage. |
| `/tmp/alfred-disney-app-secret.json` | `{"appId":"…","password":"…"}` | The bot's Entra app id + secret. |
| `/tmp/alfred-disney-vm-admin-pass.txt` | single line | VM admin password (for the `azureuser` service account). |
| `/tmp/alfred-disney-speech-key.txt` | single line | Azure Speech key. |

The fastest way to *recover* these from a healthy VM is to dump
`appsettings.production.json` via Run Command (see §6.3), but treat
that as a recovery path, not normal operations.

### 4.4 Teams app manifest

`manifest/alfred-sandbox.zip` is the artifact you upload to the Teams
Developer Portal at `https://dev.teams.microsoft.com/apps`. The
**publisher is unverified**, so an M365 admin must grant org-wide
consent in **Teams Admin Center → Manage apps → Permissions** before
the bot can join meetings or read channels.

Currently at version `1.0.7` with 11 RSC permissions: 5 chat-scoped
(meeting flow) + 6 team-scoped (channel flow). See §11 if you change
permissions — manifest version must bump and the zip must be
regenerated.

---

## 5. How to run locally

### 5.1 Python sink

```bash
cd python
uv run python run_variant_sink.py --instance dev --port 8765 \
  --product-spec batcave_platform/specs/alfred.yaml
```

The sink listens on `127.0.0.1:8765`. The agent path is wired up
end-to-end as long as `OPENAI_API_KEY` (or the equivalent provider
key) is set.

To exercise the **outbound chat** tool against a fake bot during dev,
set `BOT_SEND_CHAT_URL=http://127.0.0.1:3978/api/send-chat` before
starting the sink. Without it the tool dry-runs (logs + appends to
ledger but doesn't HTTP POST).

### 5.2 React UI

```bash
cd web && npm run dev    # http://127.0.0.1:5173
```

Hardcoded sink URL is in `web/src/api.ts` — usually points at the
Container App by default. Override with `VITE_SINK_URL` env var if
testing against a local sink.

### 5.3 C# bot — never run locally

The Graph Communications Media SDK requires Windows + cert TLS + media
ports + tenant-level Teams calling permissions. It runs only on the
Windows VM. Local C# work is "edit + docker build to verify compile +
push + deploy" — see §6.

---

## 6. Deploy

### 6.1 Container Apps (Python sink + React UI) — fast, no secrets needed

```bash
TAG=$(git rev-parse --short HEAD)
az acr build --registry acralfreddisneye02c0038 \
  --image ca-alfred-api:$TAG --file python/Dockerfile python/
az containerapp update -n ca-alfred-api -g rg-alfred-disney \
  --image acralfreddisneye02c0038.azurecr.io/ca-alfred-api:$TAG

az acr build --registry acralfreddisneye02c0038 \
  --image ca-alfred-web:$TAG --file web/Dockerfile web/
az containerapp update -n ca-alfred-web -g rg-alfred-disney \
  --image acralfreddisneye02c0038.azurecr.io/ca-alfred-web:$TAG
```

Use `az acr build` (cloud build) rather than local `docker build` so
the image is in the registry without a push step. ~30-60 s for the
Python image.

### 6.2 VM bot — full bootstrap path (`scripts/deploy-azure-vm.sh`)

This is the canonical deploy. It does Phase 1 (config + publish), Phase
2 (firewall), Phase 3 (cert), Phase 4 (service start), Phase 5 (smoke).
Requires all four secret files in `/tmp/` (see §4.3).

```bash
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
```

`SKIP_REPO_SYNC=0` is required after a code push. Use `=1` only for
config-only redeploys.

### 6.3 VM bot — incremental deploy without secret files

When you only need to push new code and don't want to re-run the full
bootstrap, use this pattern. It pulls latest, patches
`appsettings.production.json` in place, force-clean rebuilds, restarts
the service. **No secret files required**, just `az` access:

```bash
# Write the deploy script to /tmp first (see scripts/incremental-deploy-template.ps1
# or recreate from this session's /tmp/alfred-channel-deploy.ps1).
# Critical bits in the script:
#   - $ErrorActionPreference = "Continue"  (NOT "Stop")
#   - SSH key path uses forward slashes: "C:/ProgramData/alfred/deploy_key"
#   - Picks remote dynamically (origin OR private — VM may have either)
#   - Removes bin/ and obj/ before publish (forces fresh build; see §7.4)

az vm run-command create \
  -g rg-alfred-disney --vm-name vm-alfred-disney --location eastus \
  --run-command-name <unique-name> \
  --script "$(cat /tmp/incremental-deploy.ps1)" \
  --async-execution false --timeout-in-seconds 1500 --output none

# Inspect result:
az vm run-command show -g rg-alfred-disney --vm-name vm-alfred-disney \
  --run-command-name <unique-name> --instance-view \
  --query "{state:instanceView.executionState, exit:instanceView.exitCode, error:instanceView.error}" -o json

# CLEAN UP — orphaned run-commands wedge the extension (see §7.5).
az vm run-command delete -g rg-alfred-disney --vm-name vm-alfred-disney \
  --run-command-name <unique-name> --yes
```

### 6.4 After deploy — quick health check

```bash
BOT=https://alfred-disney-bot.eastus.cloudapp.azure.com
SINK=https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io
WEB=https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io

curl -sS -m 10 $BOT/api/calling/health | jq
curl -sS -m 10 $SINK/health | jq
curl -sS -m 10 -o /dev/null -w "web HTTP=%{http_code}\n" $WEB

# New since channel work:
curl -sS -m 10 $BOT/api/channels | jq
curl -sS -m 10 $SINK/openapi.json | jq '.components.schemas.TranscriptEventRequest.properties | keys'
```

---

## 7. The hard-won debug knowledge (read this first when something breaks)

### 7.1 Branch divergence and parallel histories

This repo's three remotes regularly produce *parallel histories* — the
same commits show up on different remotes with different SHAs because
of cross-remote rebases. `git push` will fail with "non-fast-forward"
even though the content is identical.

Diagnose:
```bash
git log --oneline private/main..main | head      # local-only
git log --oneline main..private/main | head      # remote-only
git diff main..private/main --stat               # actual content diff
```

If `git diff` is empty, content is fine, just SHAs differ. To recover
without losing work:
```bash
git stash push --include-untracked --message "WIP"
git fetch private
git reset --hard private/main      # local now matches remote
git stash pop                       # re-apply your changes
git add ... && git commit ...
git push origin main && git push private main
```

For the Disney gitlab branch (which gets force-pushed regularly for
MR review), `--force-with-lease` is fine:
```bash
git push disney main:alfred-agent-updates --force-with-lease
```

### 7.2 PowerShell gotchas when running scripts via `az vm run-command`

When you pass a PowerShell script via:
```bash
az vm run-command create --script "$(cat /tmp/foo.ps1)"
```
the script body goes through bash → JSON → PowerShell. Two things bite
you every single time:

**(a) Backslashes vanish.** A path like `C:\ProgramData\alfred\deploy_key`
arrives on the VM as `C:ProgramDataalfreddeploy_key` because the
escape-handling between layers strips them. **Use forward slashes**
inside the script — Git, OpenSSH, and PowerShell `Get-Content` all
accept them on Windows:
```powershell
$key = "C:/ProgramData/alfred/deploy_key"
```

**(b) Native command stderr terminates the script** when
`$ErrorActionPreference = "Stop"`. Even harmless lines like
`From github.com:logan-robbins/...` from `git fetch` count.
Set `$ErrorActionPreference = "Continue"` and gate failure on
`$LASTEXITCODE` instead:
```powershell
$ErrorActionPreference = "Continue"
& git fetch origin
if ($LASTEXITCODE -ne 0) { throw "git fetch failed" }
```

### 7.3 Run-command names cache results

If you `az vm run-command create` with the same `--run-command-name`
that's already been used, you may get the *previous* result back —
the create silently updates the existing resource and returns its
prior `instanceView`. Symptom: you fixed the script but keep seeing
the old error.

**Always** either:
- Use a unique name per attempt (`channel-deploy-v2`, `…-v3`, …), or
- Delete first, then create:
  ```bash
  az vm run-command delete -g rg-alfred-disney --vm-name vm-alfred-disney \
    --run-command-name $NAME --yes 2>&1 || true
  az vm run-command create … --run-command-name $NAME …
  ```

### 7.4 `dotnet publish` ships stale binaries

This was the most painful debug of the session. Symptom: source on the
VM matches the latest commit, `Program.cs` clearly contains the new
function `ResolveCertificate`, but the running service crashes with
a stack frame referencing the *old* function `LoadCertificateFromStore`.

Cause: `dotnet publish` is incremental. With `bin/` and `obj/` left
over from a previous build at the same commit, MSBuild decides
"nothing changed" and emits a DLL whose timestamp is *new* but whose
content is *old*.

**Fix** (always do this in any deploy script that touches code):
```powershell
foreach ($dir in @("bin","obj")) {
    $p = Join-Path $projectRoot "src\$dir"
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
dotnet restore
dotnet publish --configuration Release --output $publishDir
```

Note: `--no-incremental` is **not** a valid `dotnet publish` flag. The
clean is the only mechanism.

### 7.5 Wedged Run Command extension

If you see `Conflict: Run command extension execution is in progress`
or `provisioningState=Updating` that never settles, the extension is
wedged — usually after a force-deleted action Run Command (the deprecated
`az vm run-command invoke` variant). Recovery:

1. RDP into the VM.
2. Stop services `WindowsAzureGuestAgent` and `RdAgent`.
3. Delete `C:\Packages\Plugins\Microsoft.CPlat.Core.RunCommandWindows*`.
4. Start the services.

`az vm run-command create` (the *managed* variant) is the only
sanctioned path. Never use `az vm run-command invoke`. (See
`auto-memory/feedback_run_command_extension_orphans.md`.)

### 7.6 ARM instance view lag

`az vm get-instance-view` returns `vmAgent: null` even when the agent
is fine — ARM cache is up to 10 minutes behind reality. Don't act on
that field. Instead, probe:
```bash
az vm run-command create -g rg-alfred-disney --vm-name vm-alfred-disney \
  --location eastus --run-command-name agent-probe \
  --script 'Write-Host alive' \
  --async-execution false --timeout-in-seconds 60 --output none
```
If that returns Succeeded, the agent is alive.

### 7.7 The auto-join saga (what every "Alfred didn't join" report actually means)

Most "auto-join" issues aren't really about auto-join at the bot
level — they're configuration mismatches. Tier the diagnosis:

**Tier 1: did Graph even create the call?**
```bash
curl -sS -D - -X POST $BOT/api/calling/join \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg joinUrl "$JOIN_URL" \
        '{joinUrl:$joinUrl, displayName:"Alfred", joinMode:"invite_and_graph_join", botAttendeePresent:true}')"
```
- `200` + `callId` → Graph accepted, async work in progress.
- `202 deferred:true` → policy-based auto-invite path; bot waits for
  Teams to invite it. Tenant policy must include the bot.
- `400 BOT_NOT_INVITED` → invite mode requires the bot/service account
  on the meeting invite, missing.
- `403 GRAPH_PERMISSION_MISSING` / `TENANT_NOT_ENABLED_FOR_MODE` →
  RSC consent or tenant flag issue.
- `502 CALL_JOIN_FAILED_7504_OR_7505` → tenant-level Graph calling
  authorization constraint (talk to tenant admin).

**Tier 2: bot says it joined but no audio.** Check `/api/calling/health`:
```json
{
  "calls": [{
    "readiness": "ready",                  // GOOD
    "unmixed_audio_frames": 142,
    "primary_mixed_audio_frames": 0,
    "recent_peak_sample": 1247
  }]
}
```
Other readiness states:
- `unmixed_audio_missing` → Teams isn't supplying per-speaker buffers
  for this meeting; speaker-identity mapping won't work but transcripts
  may still flow if `primary_mixed_audio_frames > 0`.
- `media_not_flowing` → joined but media socket cold; usually a 7504.
- `silent_audio` → media is alive, peak == 0, no one is unmuted in
  Teams or app/bot audio is suppressed in meeting options. Verify the
  bot is in the roster, the meeting allows app audio, and someone is
  actually talking.

**Tier 3: audio arrives but no transcripts.** Check VM logs for
`Azure session started`, transcription cancellation messages, and
sink `events_received` increasing. Most common cause: Speech key
rotated and bot still has the old one.

VM log tail:
```bash
az vm run-command create -g rg-alfred-disney --vm-name vm-alfred-disney \
  --location eastus --run-command-name tail-logs \
  --script 'Get-Content C:\teams-bot-poc\logs\service-output.log -Tail 80; Write-Host "---STDERR---"; Get-Content C:\teams-bot-poc\logs\service-error.log -Tail 40 -ErrorAction SilentlyContinue' \
  --async-execution false --timeout-in-seconds 60 --output none
az vm run-command show -g rg-alfred-disney --vm-name vm-alfred-disney \
  --run-command-name tail-logs --instance-view --query "instanceView.output" -o tsv
az vm run-command delete -g rg-alfred-disney --vm-name vm-alfred-disney \
  --run-command-name tail-logs --yes
```

### 7.8 Graph Communications Media SDK Windows prereqs

If the bot service starts but logs `DllNotFoundException: NativeMedia`,
the VM is missing one of:
- `Server-Media-Foundation` Windows feature (`Install-WindowsFeature
  Server-Media-Foundation` then reboot).
- VC++ 2015-2022 Redistributable.

The bootstrap script installs both; just re-run `deploy-azure-vm.sh`.

`MediaPlatform needs at least 2 cores` means the VM SKU is too small.
Resize to ≥ 2 physical cores: `az vm resize -g rg-alfred-disney
-n vm-alfred-disney --size Standard_D4s_v3`.

### 7.9 Cert auto-renewal

The bot resolves its TLS cert at startup with this fallback chain:
1. configured thumbprint
2. Subject CN matches `MediaPlatformSettings.ServiceFqdn`
3. FriendlyName starts with `CertificateFriendlyName`

So when `win-acme` auto-renews and stamps a new thumbprint, the bot
finds the new cert by Subject/FriendlyName on next restart — no manual
intervention needed. If you ever do see `Certificate with thumbprint
'…' not found`, the cert is genuinely missing; re-run the bootstrap
script which installs win-acme + requests a fresh Let's Encrypt cert.

---

## 8. API surface — what's where

### 8.1 Bot HTTP API (port 443 on the VM)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/messages` | Bot Framework activity ingress (chat) |
| `POST` | `/api/calling` | Graph calling webhook |
| `GET` | `/api/calling/health` | Per-call readiness snapshots |
| `POST` | `/api/calling/join` | Manually trigger a call join |
| `POST` | `/api/send-chat` | Any consumer calls this to post into a Teams chat. Body must echo `conversation_reference_id` from the envelope. |
| `POST` | `/api/graph-notifications` | Graph change-notification webhook |
| `GET` | `/api/channels` | List persistent channel attachments |
| `POST` | `/api/channels/attach` | Attach Alfred to a Teams channel |
| `DELETE` | `/api/channels/{teamId}/{channelId}` | Detach |
| `GET` | `/api/channels/{teamId}/{channelId}/consumers` | List the per-channel consumer URLs. |
| `PUT` | `/api/channels/{teamId}/{channelId}/consumers` | Replace the entire consumer list. |
| `POST` | `/api/channels/{teamId}/{channelId}/consumers` | Insert/replace one consumer by name. |
| `DELETE` | `/api/channels/{teamId}/{channelId}/consumers/{name}` | Remove one consumer by name. |

The bot's `EventFanoutDispatcher` POSTs every event for a channel to
every registered consumer URL using the
[`alfred-events-v1` envelope shape](docs/event-contract.md). Each
team's backend lives behind one of those URLs; this repo's Python
sink is one such reference consumer.

### 8.2 Sink HTTP API (Container App)

This sink is the **reference consumer** for the
[`alfred-events-v1` contract](docs/event-contract.md). It happens to
power Disney's React UI, but other teams stand up their own backends
behind their own URLs and consume the same envelope shape.

Routing is all keyed on `chat_thread_id`:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/events` | **Versioned envelope ingress** — the only event ingress. Routes by `event_type` to internal handlers (transcript / chat / session_linked). |
| `GET` | `/session/link/{tid}` | Fetch a thread's channel link |
| `GET` | `/channels/links` | List all session→channel links |
| `GET` | `/c/{teamId}/{channelId}/events` | **Channel rollup** — every event under a channel, ordered by ts |
| `GET` | `/m` | List active meetings |
| `GET` | `/m/{tid}/status` | Per-meeting state (session + history) |
| `GET` | `/m/{tid}/events` | SSE stream of ledger appends |
| `GET` | `/m/{tid}/ledger` | Persisted ledger snapshot |
| `GET` | `/m/{tid}/dossier` | Decisions / open questions / action items / risks |
| `POST` | `/m/{tid}/mute` | Mute Alfred for one meeting |
| `POST` | `/m/{tid}/end` | End a meeting session |
| `GET` | `/sessions/{sid}/raw-events` | E1 raw audit log |
| `GET` | `/sessions/{sid}/raw-events/export.ndjson` | NDJSON export |
| `GET` | `/sessions/{sid}/participants` | Roster |
| `GET` | `/sessions/{sid}/speaker-identity` | Speaker→AAD links |
| `POST` | `/sessions/{sid}/speaker-mapping` | Manual override |
| `GET` | `/health` | Liveness |

The OpenAPI spec is auto-generated at `$SINK/openapi.json` — read it
for the canonical schema of every request body.

---

## 9. Data shape — what's in SQLite

Five canonical tables live at
`python/output/alfred-disney-sandbox-prod/alfred.sqlite3`:

```
sessions                    one row per meeting
meeting_events              normalized ledger (speech / chat / system)
raw_ingest_events           immutable audit (every inbound event before filters)
session_channel_links       chat_thread_id ↔ (team_id, channel_id, channel_thread_id)
extractions                 one row per AlfredExtraction
tool_calls                  one row per agent tool invocation
dossier_items               latest decisions/questions/action_items/risks
meeting_participants        roster
participant_msi_bindings    MSI ↔ AAD
speaker_identity_links      speaker_N ↔ AAD (E3)
```

Every event row carries optional `team_id`, `channel_id`, and
`channel_thread_id`. So:

```sql
-- All events in a Teams channel (chat + every meeting + every transcript),
-- ordered by time:
SELECT timestamp_utc, kind, source, text
FROM meeting_events
WHERE channel_id = '19:abc@thread.tacv2'
ORDER BY timestamp_utc ASC;

-- Same as above via HTTP:
GET /c/{teamId}/{channelId}/events?since=&until=&kinds=speech,chat
```

Schema migrations are *additive* in `_migrate` (the same method that
ran when you bumped the schema last). Don't put indexes on
newly-added columns inside the main `SCHEMA` string — they'll fail
on existing DBs because `executescript` runs before the migration adds
the column. Indexes go in `_migrate`.

---

## 10. Channel attachment — the persistent listen+post path

Alfred can be attached to a Teams channel persistently. Two paths in:

**Path A: Team install (preferred).** Install the app at the team
level via Teams Admin Center → Manage apps → Install. The bot receives
a `membersAdded` event; `AlfredBot.OnMembersAddedAsync` reads
`TeamsChannelData`, auto-attaches, and creates the Graph subscription.

**Path B: Operator API.** When you know the team's AAD group id and
the channel id (Teams clients → "Get link to channel" gives you both):

```bash
curl -sS -X POST $BOT/api/channels/attach \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg tid "$TEAM_ID" --arg cid "$CHANNEL_ID" \
        '{team_id:$tid, channel_id:$cid, source:"manual_attach"}')"
```

Either path:
- Persists `(teamId, channelId)` to
  `C:\teams-bot-poc\state\channel-attachments.json` on the VM.
- Creates a Graph change-notification subscription on
  `teams/{teamId}/channels/{channelId}/messages`.
- Re-issues the subscription on bot restart (`IHostedService`).
- Hands every channel post to the sink as a `conversation_kind:"channel"`
  chat event.

For meetings spawned from an attached channel:
- The meeting's chat is a *separate* thread (`19:meeting_xxx@thread.v2`),
  so it becomes its own session in the sink.
- When `AlfredBot.OnMessageActivityAsync` first sees a message in that
  meeting whose `channelData` points back at the parent channel, it
  POSTs `/session/link` to the sink, binding the meeting session to
  the channel. The sink backfills prior events.
- After that, `GET /c/{teamId}/{channelId}/events` returns channel
  posts AND every meeting's events under one ordered timeline.

---

## 11. Editing rules — do not violate

These are stable invariants. Breaking them costs hours of debug time
later.

1. **Single canonical meeting ledger.** `InterviewSession.meeting_events`,
   one session per `chat_thread_id`. The immutable layer is
   `raw_ingest_events`; back-link new ledger rows via
   `source_raw_event_ids`.
2. **`chat_thread_id` is THE meeting key.** Every transcript and chat
   must carry it; the UI URL must require it (`/m/<chat_thread_id>`).
3. **One inbound chat path.** Bot Framework activities → `/api/messages`,
   forwarded to the Python `/chat` endpoint.
4. **Outbound Alfred chat goes through `send_to_meeting_chat`.**
   No parallel output route. No `SEND/ASK/SILENT` enum on the
   extraction.
5. **`alfred.yaml` is the only source of truth** for Alfred's prompt
   and intervention policy. `AlfredAnalyzer` raises at construction if
   `instructions` is not provided — do not add a code-side default.
6. **All persistent writes** go through
   `meeting_agent.persistence.SessionStore`.
7. **Bot self-resolves its TLS cert at startup.** Cert auto-renewal
   must remain transparent.
8. **One canonical implementation per concern.** No duplicate files,
   no `v2` copies, no override-with-fallback patterns.
9. **Fail fast** when prerequisites are unmet — clear, specific
   errors at boundaries.
10. **Never glob/grep generated dirs** (`.venv`, `node_modules`,
    `bin/`, `obj/`, `__pycache__`, `*.egg-info`).
11. **`dotnet publish` always after `rm -rf bin obj`** in deploy
    scripts — see §7.4.
12. **Manifest changes** require: bump `version`, regenerate the zip
    (`cd manifest && rm alfred-sandbox.zip && zip -j alfred-sandbox.zip
    manifest.json color.png outline.png`), re-import in Teams Developer
    Portal, possibly re-grant admin consent.

---

## 12. Disney environment quick reference

| Thing | Value |
|---|---|
| Subscription | `e02c0038-82c8-4655-9647-38083f301099` (WDI R&D) |
| M365 tenant | `38387f0b-9a6f-46e2-8373-67422f8c2cb0` (plutosdoghouse.com) |
| Resource group | `rg-alfred-disney` |
| Bot VM | `vm-alfred-disney` |
| Bot host | `alfred-disney-bot.eastus.cloudapp.azure.com` |
| Bot AppId | `207a38a4-67c5-4ef9-ada8-ea7998734d59` |
| Azure Bot Service | `bot-alfred-disney` (SingleTenant) |
| Sink (Container App) | `https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io` |
| UI (Container App) | `https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io` |
| ACR | `acralfreddisneye02c0038.azurecr.io` |
| Azure OpenAI | `aoai-alfred-disney` (`gpt-5-mini`) |
| Speech Services | `speech-alfred-disney` (eastus) |

---

## 13. Common task recipes

### 13.1 "Add a new column to `meeting_events`"

1. Add the column to the `CREATE TABLE meeting_events` block in
   `python/meeting_agent/persistence.py` (this only matters for fresh
   DBs).
2. Add the same column to the `meeting_additive` list in `_migrate`
   (this is what runs on existing DBs).
3. Add the field to `MeetingEvent` (`python/meeting_agent/models.py`).
4. Update `append_meeting_event` to write the column.
5. Update `get_ledger` (and `get_channel_ledger` if relevant) to SELECT
   it back.
6. If a new index references this column, put the `CREATE INDEX` in
   `_migrate` only (NOT the main `SCHEMA` string) — see §9.
7. Wipe `python/output/alfred-test/alfred.sqlite3` so tests start
   fresh; run `uv run pytest tests`.

### 13.2 "Add a new endpoint to the sink"

1. Add the route under the existing `@app.get`/`@app.post` block in
   `transcript_sink.py`.
2. Use `state["store"]` for SQL, `state["session_registry"]` for
   live state.
3. For long DB calls, wrap in `asyncio.to_thread(...)`.
4. Add a test in `python/tests/test_sink.py` mirroring the existing
   `TestChannelLinkAndRollup` style.
5. `uv run pytest tests` should still be green.

### 13.3 "Modify the C# bot and deploy"

1. Edit C# code under `src/`.
2. Verify locally (cheap and fast):
   ```bash
   docker run --rm -v "$(pwd):/work" \
     -v alfred-nuget-cache:/root/.nuget/packages \
     -w /work/src mcr.microsoft.com/dotnet/sdk:8.0 \
     dotnet build --configuration Release --nologo /v:m
   ```
3. Commit + push to all three remotes (§4.2).
4. Deploy via `deploy-azure-vm.sh` (full) or the incremental script
   pattern in §6.3.
5. **Do not** trust the build's "succeeded" line alone; check
   `/api/calling/health` is `Healthy` after restart and tail the
   service log (§7.7).

### 13.4 "Channel-attached bot in a sandbox tenant — full setup"

1. Re-import `manifest/alfred-sandbox.zip` (must be 1.0.7+) in the
   Teams Developer Portal.
2. Have the M365 admin consent to RSC permissions in Teams Admin Center.
3. Install the app at the team level. The bot auto-attaches.
4. Verify: `curl $BOT/api/channels | jq` shows the attachment.
5. Post in the channel — `curl $SINK/c/{teamId}/{channelId}/events | jq`
   should show the post within seconds.

---

## 14. Files this agent should never create

The user's CLAUDE.md prohibits unsolicited markdown artifacts. Don't
create `SUMMARY.md`, `final-guide.md`, `notes.md`, or similar unless
explicitly asked. Updating existing operational docs (`README.md`,
`PROD.md`, this file) is fine when warranted by a feature change.

---

## 15. Version pointers

- This file describes the repo as of commit at the time of writing.
  When in doubt, `git log --oneline -20` on `main` and verify any
  endpoints/scripts mentioned still exist.
- `PROD.md` tracks deferred productionalization work.
- `README.md` is the product overview; this file is the operational
  layer underneath it.
