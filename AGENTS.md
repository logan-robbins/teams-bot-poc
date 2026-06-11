# AGENTS.md — Operator manual for AI coding agents

Debug + deploy companion to `README.md`. Read README first for product
context.

---

## 1. Mental model

```
Teams
  |
  v
C# bot (Windows VM, Graph Media SDK + Bot Framework)
  |
  +--> BlobEventArchive PUTs alfred-v2 JSON to stalfreddisney/alfred-events
  |
  +--> EventFanoutDispatcher POSTs alfred-v2 JSON to consumer URLs
         |
         +--> Our Alfred: Python sink POST /v2/events (FastAPI on ACA)
         |
         +--> Client Alfred: server_v2.py or a customer service POST /v2/events
```

- **C# bot** on `vm-alfred-disney`. Graph Media SDK is Windows-only and
  needs Server-Media-Foundation.
- **C# bot** is the capture + delivery platform. It writes blob storage
  and posts live consumer URLs as sibling outputs; it does not write
  blob storage through the Python sink.
- **Python sink** is our Alfred implementation. It owns session state,
  intervention policy, dossier,
  PostgreSQL ledger (`pg-alfred-disney`).
- **React UI** is read-only; SSE consumer.
- **Client sidecars** such as `server_v2.py` consume the same rails
  without changing the core bot or sink.

`chat_thread_id` ties everything together
(`19:meeting_<base64>@thread.v2` for meetings,
`19:{channelId}@thread.tacv2` for channels). UI URL is
`/m/<chat_thread_id>`. Every event also carries optional `team_id` /
`channel_id` / `channel_thread_id` for channel-wide analytics without
joins.

---

## 2. Repo layout

| Path | Owner | Build | Deploy |
|---|---|---|---|
| `src/` | C# Teams bot | `dotnet publish` (.NET 8, win-x64) | `vm-alfred-disney`, NSSM service `TeamsMediaBot` |
| `python/` | FastAPI sink + Alfred agent | `uv sync` / `uv run` | `ca-alfred-api` Container App |
| `web/` | React 19 + Vite UI | `npm install && npm run build` | `ca-alfred-web` Container App |
| `manifest/` | Teams app manifest | `zip -j alfred-sandbox.zip manifest.json color.png outline.png` | Teams Developer Portal |
| `scripts/` | deploy + ops scripts | n/a | n/a |

Canonical concepts:
- **Session state**: `InterviewSession` in `meeting_agent/models.py`.
- **Persistent ledger**: PostgreSQL. DSN = `ALFRED_DB_URL` (prod:
  `pg-alfred-disney.postgres.database.azure.com:5432/alfred`, sourced
  from Container App secret `alfred-db-url`). Schema +
  `psycopg[binary]` + `psycopg_pool.ConnectionPool` (min=1, max=5,
  autocommit, dict_row) in `meeting_agent/persistence.py`.
- **Sole outbound**: `send_to_meeting_chat` in `meeting_agent/tools.py`.
  No parallel path.
- **Prompt + intervention policy**: `batcave_platform/specs/alfred.yaml`
  is the only source of truth.

---

## 3. Environment setup (macOS / Apple Silicon dev box)

### 3.1 Required tools

```bash
brew list | grep -E '^(uv|jq|gh|node|docker)$'   # uv, jq, gh, node, docker
az --version | head -3
az login
az account set --subscription e02c0038-82c8-4655-9647-38083f301099
```

No local `dotnet` (see §3.3).

### 3.2 Python (`uv` only — never `pip`, never raw `python`)

```bash
cd python
uv sync
uv run pytest tests -q
uv run python run_variant_sink.py --instance dev --port 8765 \
  --product-spec batcave_platform/specs/alfred.yaml
```

`pip install`, `python script.py`, `source .venv/bin/activate` are
wrong in this repo. Use `uv add <pkg>` and `uv run`.

### 3.3 C# build — Docker with a named NuGet volume

Fresh restore ~12 min; cached: seconds. Always use the volume.

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

Production build runs on the Windows VM during deploy — this local
build is only for catching compile errors before pushing.

### 3.4 Web

```bash
cd web && npm install && npm run dev    # http://127.0.0.1:5173
```

---

## 4. Login + secrets

### 4.1 Git remotes

| Remote | URL | Role |
|---|---|---|
| `origin` | `github.com/logan-robbins/teams-bot-poc` | Public mirror |
| `private` | `github.com/logan-robbins/alfred-teams-bot` | VM source of truth (deploy key references this) |
| `disney` | `gitlab.wdi.disney.com/Michael.Barron.-ND/teams_integration` | Disney internal review; `alfred-agent-updates` MR branch |

Push protocol after a feature commit:
```bash
git push origin main
git push private main
git push disney main:alfred-agent-updates --force-with-lease
```

The disney remote uses an embedded PAT — treat it as opaque. SSH for
`private`/`origin` uses `~/.ssh` keys.

### 4.2 Deploy secrets (Disney sandbox)

`scripts/deploy-azure-vm.sh` expects these in `/tmp/`:

| File | Format | Source |
|---|---|---|
| `/tmp/alfred-deploy-key` | SSH private key | Public half = GitHub deploy key |
| `/tmp/alfred-disney-app-secret.json` | `{"appId":"…","password":"…"}` | Bot Entra app |
| `/tmp/alfred-disney-vm-admin-pass.txt` | single line | VM admin password (`azureuser`) |
| `/tmp/alfred-disney-speech-key.txt` | single line | Azure Speech key |

Recovery: dump `appsettings.production.json` from a healthy VM via Run
Command (§6.3).

### 4.3 Teams app manifest

`manifest/alfred-sandbox-v1.0.12.zip` uploads to Teams Developer Portal at
`https://dev.teams.microsoft.com/apps`. Publisher is unverified — M365
admin must grant org-wide consent in Teams Admin Center → Manage apps
→ Permissions. Currently `1.0.12` with 16 RSC permissions (7 chat + 9
team). Version-bump rule in §11.

---

## 5. How to run locally

**Python sink** — listens on `127.0.0.1:8765`. Needs `OPENAI_API_KEY`
(or provider equivalent). Set `BOT_SEND_CHAT_URL=http://127.0.0.1:3978/api/send-chat`
to exercise outbound chat against a fake bot; without it the tool
dry-runs (logs + ledger, no POST).
```bash
cd python
uv run python run_variant_sink.py --instance dev --port 8765 \
  --product-spec batcave_platform/specs/alfred.yaml
```

**React UI** — default sink URL in `web/src/api.ts`; override with
`VITE_SINK_URL` for local sink testing.
```bash
cd web && npm run dev    # http://127.0.0.1:5173
```

**C# bot** — never run locally. Graph Media SDK needs Windows + cert
TLS + media ports + tenant-level Teams calling permissions. Edit →
docker compile-check → push → deploy (§6).

---

## 6. Deploy

### 6.1 Container Apps (Python sink + React UI)

`az acr build` is cloud build (skips local docker push). ~30-60 s per
image.

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

#### 6.1.1 Persistent-sink wiring (critical)

`ca-alfred-api` persists to `pg-alfred-disney` (Azure Database for
PostgreSQL Flexible Server, `Standard_B1ms`, eastus2, db `alfred`,
PG 16, 32 GB storage). The connection string lives in the Container
App secret `alfred-db-url` and is surfaced to the container via the
`ALFRED_DB_URL` env var. Firewall rule
`AllowAllAzureServicesAndResourcesWithinAzureIps` is what lets ACA
reach the server.

Wire (one-time, idempotent):
```bash
SUB=e02c0038-82c8-4655-9647-38083f301099
DSN='postgresql://alfredadmin:<password>@pg-alfred-disney.postgres.database.azure.com:5432/alfred?sslmode=require'

az containerapp secret set --subscription $SUB \
  -n ca-alfred-api -g rg-alfred-disney \
  --secrets alfred-db-url="$DSN"

az containerapp update --subscription $SUB \
  -n ca-alfred-api -g rg-alfred-disney \
  --set-env-vars ALFRED_DB_URL=secretref:alfred-db-url
```

Verify:
```bash
az postgres flexible-server show --subscription $SUB \
  -g rg-alfred-disney -n pg-alfred-disney \
  --query '{state:state, version:version, sku:sku.name, fqdn:fullyQualifiedDomainName}' -o json
az postgres flexible-server firewall-rule list --subscription $SUB \
  -g rg-alfred-disney -n pg-alfred-disney \
  --query "[].{name:name, start:startIpAddress, end:endIpAddress}" -o json
az containerapp secret list --subscription $SUB \
  -n ca-alfred-api -g rg-alfred-disney \
  --query "[?name=='alfred-db-url'].name" -o tsv
az containerapp show --subscription $SUB \
  -n ca-alfred-api -g rg-alfred-disney \
  --query "properties.template.containers[0].env[?name=='ALFRED_DB_URL']" -o json
```

The old `sink-data` volume + `STORE_DB_PATH=/var/lib/alfred/alfred.sqlite3`
wiring is gone. The volume declaration may still exist in the template
but is unused; the `stalfreddisney/alfred-sink-data` file share is
dormant. Don't restore that pattern (§7.11).

### 6.2 VM bot — full bootstrap (`scripts/deploy-azure-vm.sh`)

Canonical deploy. Phases: config+publish, firewall, cert, service
start, smoke. Requires all four `/tmp/` secret files (§4.2).
`SKIP_REPO_SYNC=0` after a code push; `=1` for config-only.

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

### 6.3 VM bot — incremental deploy without secret files

Pulls latest, patches `appsettings.production.json`, force-clean
rebuilds, restarts the service. No secret files; just `az` access.

**Pre-flight: prune Succeeded run-commands.** The VM caps at **25
managed Run Commands**. Once the cap is hit, `az vm run-command create`
silently fails with `BadRequest`, then `ResourceNotFound` when you
query the new name. Always prune first:

```bash
az vm run-command list --subscription e02c0038-82c8-4655-9647-38083f301099 \
  --vm-name vm-alfred-disney -g rg-alfred-disney \
  --query '[?provisioningState==`Succeeded`].name' -o tsv \
  | while read n; do \
      az vm run-command delete --subscription e02c0038-82c8-4655-9647-38083f301099 \
        --vm-name vm-alfred-disney -g rg-alfred-disney \
        --run-command-name "$n" --yes; \
    done
```

PowerShell script must use: `$ErrorActionPreference = "Continue"`
(not `"Stop"`), forward slashes for SSH key path
(`"C:/ProgramData/alfred/deploy_key"`), dynamic remote pick (origin OR
private), `rm -rf bin obj` before publish (§7.4).

```bash
az vm run-command create \
  -g rg-alfred-disney --vm-name vm-alfred-disney --location eastus \
  --run-command-name <unique-name> \
  --script "$(cat /tmp/incremental-deploy.ps1)" \
  --async-execution false --timeout-in-seconds 1500 --output none

az vm run-command show -g rg-alfred-disney --vm-name vm-alfred-disney \
  --run-command-name <unique-name> --instance-view \
  --query "{state:instanceView.executionState, exit:instanceView.exitCode, error:instanceView.error}" -o json

# Clean up — wedges the extension otherwise (§7.5).
az vm run-command delete -g rg-alfred-disney --vm-name vm-alfred-disney \
  --run-command-name <unique-name> --yes
```

### 6.4 After deploy — health check

```bash
BOT=https://alfred-disney-bot.eastus.cloudapp.azure.com
SINK=https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io
WEB=https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io

curl -sS -m 10 $BOT/api/calling/health | jq
curl -sS -m 10 $SINK/health | jq
curl -sS -m 10 -o /dev/null -w "web HTTP=%{http_code}\n" $WEB

curl -sS -m 10 $BOT/api/channels | jq
curl -sS -m 10 $SINK/openapi.json | jq '.components.schemas | keys'
```

---

## 7. The hard-won debug knowledge (read first when something breaks)

### 7.1 Branch divergence and parallel histories

The three remotes regularly produce parallel histories — same content,
different SHAs, from cross-remote rebases. `git push` fails
"non-fast-forward" even when content is identical.

```bash
# Diagnose: if `git diff` is empty, content is fine.
git log --oneline private/main..main | head
git log --oneline main..private/main | head
git diff main..private/main --stat

# Recover without losing work:
git stash push --include-untracked --message "WIP"
git fetch private
git reset --hard private/main
git stash pop
git add ... && git commit ...
git push origin main && git push private main

# Disney gitlab branch gets force-pushed for MR review:
git push disney main:alfred-agent-updates --force-with-lease
```

### 7.2 PowerShell gotchas in `az vm run-command`

`--script "$(cat /tmp/foo.ps1)"` routes the body through bash → JSON →
PowerShell. Two things bite every time:

**(a) Backslashes vanish.** `C:\ProgramData\alfred\deploy_key` arrives
as `C:ProgramDataalfreddeploy_key`. Use forward slashes — Git, OpenSSH,
PowerShell `Get-Content` all accept them on Windows:
```powershell
$key = "C:/ProgramData/alfred/deploy_key"
```

**(b) Native command stderr terminates with `Stop`.** Even
`From github.com:logan-robbins/...` from `git fetch` counts. Use
`Continue` and gate on `$LASTEXITCODE`:
```powershell
$ErrorActionPreference = "Continue"
& git fetch origin
if ($LASTEXITCODE -ne 0) { throw "git fetch failed" }
```

### 7.3 Run-command names cache results

Reusing `--run-command-name` returns the *previous* `instanceView`.
Symptom: you fixed the script but keep seeing the old error. Use a
unique name per attempt or delete first:
```bash
az vm run-command delete -g rg-alfred-disney --vm-name vm-alfred-disney \
  --run-command-name $NAME --yes 2>&1 || true
az vm run-command create … --run-command-name $NAME …
```

### 7.4 `dotnet publish` ships stale binaries

`dotnet publish` is incremental — with stale `bin/` and `obj/`, MSBuild
emits a DLL whose timestamp is new but content is old. Source on VM
matches latest commit, running service crashes with a stack frame from
the old function. Always clean before publish:

```powershell
foreach ($dir in @("bin","obj")) {
    $p = Join-Path $projectRoot "src\$dir"
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
dotnet restore
dotnet publish --configuration Release --output $publishDir
```

`--no-incremental` is NOT a valid `dotnet publish` flag. NSSM runs
`publish/TeamsMediaBot.exe`, so you must `publish` (not `build`).

### 7.5 Wedged Run Command extension

Symptom: `Conflict: Run command extension execution is in progress` or
a `provisioningState=Updating` that never settles — usually after a
force-deleted action Run Command (deprecated `az vm run-command invoke`).
Recovery: RDP → stop `WindowsAzureGuestAgent` and `RdAgent` → delete
`C:\Packages\Plugins\Microsoft.CPlat.Core.RunCommandWindows*` → start
services. Use `az vm run-command create` (managed); never `invoke`.

### 7.6 ARM instance view lag

`az vm get-instance-view` returns `vmAgent: null` even when the agent
is fine — ARM cache is up to 10 minutes behind reality. Probe:
```bash
az vm run-command create -g rg-alfred-disney --vm-name vm-alfred-disney \
  --location eastus --run-command-name agent-probe \
  --script 'Write-Host alive' \
  --async-execution false --timeout-in-seconds 60 --output none
```
Succeeded → agent alive.

### 7.7 The auto-join saga

Most "Alfred didn't join" is a config mismatch, not auto-join. Tier:

**Tier 1: did Graph create the call?**
```bash
curl -sS -D - -X POST $BOT/api/calling/join \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg joinUrl "$JOIN_URL" \
        '{joinUrl:$joinUrl, displayName:"Alfred", joinMode:"invite_and_graph_join", botAttendeePresent:true}')"
```
- `200` + `callId` → Graph accepted.
- `202 deferred:true` → policy-based auto-invite; tenant policy must
  include the bot.
- `400 BOT_NOT_INVITED` → invite mode requires the bot/service account
  on the invite.
- `403 GRAPH_PERMISSION_MISSING` / `TENANT_NOT_ENABLED_FOR_MODE` →
  RSC consent or tenant flag.
- `502 CALL_JOIN_FAILED_7504_OR_7505` → tenant-level Graph calling
  constraint (admin).

**Tier 2: joined, no audio.** `/api/calling/health` `readiness` values:
- `ready` — good (check `unmixed_audio_frames`, `recent_peak_sample`).
- `unmixed_audio_missing` → Teams not supplying per-speaker buffers;
  speaker-identity won't map but transcripts may flow if
  `primary_mixed_audio_frames > 0`.
- `media_not_flowing` → media socket cold; usually a 7504.
- `silent_audio` → peak == 0. Verify bot is in roster, meeting allows
  app audio, someone is talking.

**Tier 3: audio, no transcripts.** Check VM logs for `Azure session
started`, transcription cancellation, sink `events_received`
increasing. Most common cause: rotated Speech key.

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

### 7.8 Graph Media SDK Windows prereqs

`DllNotFoundException: NativeMedia` means the VM is missing one of:
- `Server-Media-Foundation` Windows feature
  (`Install-WindowsFeature Server-Media-Foundation` + reboot).
- VC++ 2015-2022 Redistributable.

The bootstrap installs both; re-run `deploy-azure-vm.sh`.

`MediaPlatform needs at least 2 cores` → VM SKU too small:
```bash
az vm resize -g rg-alfred-disney -n vm-alfred-disney --size Standard_D4s_v3
```

### 7.9 Cert auto-renewal

Bot resolves TLS cert at startup via: (1) configured thumbprint,
(2) Subject CN matches `MediaPlatformSettings.ServiceFqdn`,
(3) FriendlyName starts with `CertificateFriendlyName`. When
`win-acme` auto-renews, the bot finds the new cert by
Subject/FriendlyName on next restart — no manual intervention. If you
see `Certificate with thumbprint '…' not found`, the cert is genuinely
missing; re-run the bootstrap.

### 7.10 Sidecar bridges (`server_v2.py`, `server.py`, `server_1.py`)

`server_v2.py` at the repo root is the current client-side bridge
example. It is not part of the deployed C# bot or Python sink. It keeps
the old `/chat` API, accepts live `alfred-v2` fanout at `POST
/v2/events`, can poll the canonical v2 blob archive paths for
catch-up, and sends replies through `$BOT/api/send-chat`.

`server.py` and `server_1.py` are legacy v1 polling-bridge sidecars for
comparison during migration. They are not deployed by scripts. Do not
extend their stale `teams/.../channel.message.created/` polling layout;
new consumers use `server_v2.py`, a custom HTTP consumer, or the v2
blob category layout.

### 7.11 Don't retry sqlite-over-SMB

The persistent ledger used to be sqlite on an Azure File share
(`stalfreddisney/alfred-sink-data` mounted at `/var/lib/alfred`).
Sqlite requires `fcntl` advisory locks; SMB/CIFS does not implement
them. Result: every new revision crashed at startup with
`sqlite3.OperationalError: database is locked`, ACA marked the
revision unhealthy and silently rolled traffic back to the last
healthy revision, and the bad image looked deployed in `az
containerapp show` while users kept hitting the old code (gpt-5-mini
deploy looked live for days but wasn't). Use Postgres for durable
persistence (current setup — `pg-alfred-disney`, §6.1.1) — or NetApp
NFS if a filesystem mount is unavoidable. Never put sqlite on Azure
Files again.

### 7.12 Post-meeting Microsoft transcript didn't land

Distinct from Tier 3 (live STT, audio-no-transcripts in §7.7). This
is "the meeting ended, no `official.txt` / `official.vtt` showed up
in `meetings/{mid_sanitized}/transcripts/`". Auto-fetch is wired
through `OfficialTranscriptFetcher`; this is where to look when it
silently misses.

**0. Was the meeting eligible?** Channel meetings are intentionally
skipped — `OnlineMeetingTranscript.Read.Chat` doesn't cover them and
no public Graph endpoint consumes `ChannelMeetingTranscript.Read.Group`
alone (README §7.2). The fetcher's `Register` early-returns on any
`@thread.tacv2` id with a "Skipping ... no public Graph transcripts
endpoint" log. Don't debug what Microsoft doesn't ship.

**1. Was Record-and-Transcribe on?** No transcribe → no transcript
to fetch. The fetcher will poll the full 30-min budget + 1h retry +
another 30 min, then give up with `exhausted retry`. Confirm in the
meeting organizer's Stream / OneDrive whether the meeting produced
a transcript at all.

**2. Did the fetcher register?** Grep service-output.log for one of:
- `Scheduled transcript fetch botCallId=...` — first chat sighting,
  registered at meeting start.
- `Extended transcript fetch botCallId=... new_deadline=...` —
  `OnTeamsMeetingEndAsync` re-registered, pushing the deadline to
  "30 min after end".
- `Skipping transcript fetcher for channel meeting` — guard fired
  (see step 0).

If neither Scheduled nor Extended appears: the chat-sighting handler
didn't see the chat, OR `OnTeamsMeetingEndAsync` never received the
meetingEnd activity. The latter requires `groupChat` scope in the
manifest (present in 1.0.12) plus the bot being installed in the
meeting chat (`+Apps`). Confirm with `curl $BOT/api/channels` and
the chat showing Alfred in the participants list.

**3. Is the session still in-flight or already gave up?** The
persistence file is the source of truth — every Register, every
deadline-extension, every retry-arming flushes there atomically.

```bash
az vm run-command create --subscription e02c0038-82c8-4655-9647-38083f301099 \
  -g rg-alfred-disney --vm-name vm-alfred-disney --location eastus \
  --run-command-name pending-fetches --async-execution false \
  --timeout-in-seconds 60 --output none \
  --script 'Get-Content C:\teams-bot-poc\state\pending-transcript-fetches.json -ErrorAction SilentlyContinue'
az vm run-command show --subscription e02c0038-82c8-4655-9647-38083f301099 \
  -g rg-alfred-disney --vm-name vm-alfred-disney \
  --run-command-name pending-fetches --instance-view \
  --query "instanceView.output" -o tsv
az vm run-command delete --subscription e02c0038-82c8-4655-9647-38083f301099 \
  -g rg-alfred-disney --vm-name vm-alfred-disney \
  --run-command-name pending-fetches --yes
```

Each record has `bot_call_id`, `organizer_oid`, `deadline_utc`,
`retry_used`. Interpretation:
- `deadline_utc > now` and `retry_used: false` → first poll window
  still active; just wait.
- `deadline_utc > now` and `retry_used: true` → bonus retry window
  active; transcript landed late but is still in scope.
- `deadline_utc < now` and present in the file → record was pruned
  out at startup; about to be dropped by the next persist write.
- absent and no `Emitted meeting.transcript.official` log → fetcher
  gave up. Manual backfill (step 5).

**4. Did Graph 403 / 404 the poll?** Polls log at Debug level:
`List transcripts returned Forbidden for organizer=... meeting=...`
or `... NotFound ...`. The fetcher swallows these and retries on the
next interval. Persistent 403 means the chat-scoped RSC didn't
consent — re-install Alfred via `+Apps` on that meeting chat.
Persistent 404 with the canonical meeting id means the organizer's
tenant doesn't expose the transcript via the
`useResourceSpecificConsentBasedAuthorization=true` path; check
whether `OnlineMeetingTranscript.Read.Chat` is grantable in Sandbox
at all.

**5. Operator backfill.** `POST /api/debug/fetch-transcript` fires
the same poller against a hand-supplied `meeting_id` + `organizer_oid`
with a 24h look-back window. Idempotent — re-registering a key that
already produced a transcript is a no-op.

```bash
BOT=https://alfred-disney-bot.eastus.cloudapp.azure.com
curl -sS -X POST $BOT/api/debug/fetch-transcript \
  -H 'Content-Type: application/json' \
  -d '{
    "meeting_id":"19:meeting_NmFkYW...@thread.v2",
    "organizer_oid":"38387f0b-...",
    "meeting_chat_thread_id":"19:meeting_NmFkYW...@thread.v2"
  }'
```

Watch `meetings/{mid_sanitized}/transcripts/` over the next ~2 min.
Re-tail logs with the §7.7 recipe if it stays empty.

---

## 8. API surface

### 8.1 Bot HTTP API (port 443 on the VM)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/messages` | Bot Framework activity ingress (chat) |
| `POST` | `/api/calling` | Graph calling webhook |
| `GET` | `/api/calling/health` | Per-call readiness snapshots |
| `POST` | `/api/calling/join` | Manually trigger a call join |
| `POST` | `/api/send-chat` | Outbound post into a Teams chat. Body must echo `conversation_reference_id` |
| `POST` | `/api/graph-notifications` | Graph change-notification webhook |
| `GET` | `/api/channels` | List channel attachments |
| `POST` | `/api/channels/attach` | Attach |
| `DELETE` | `/api/channels/{teamId}/{channelId}` | Detach |
| `GET / PUT / POST / DELETE` | `/api/channels/{teamId}/{channelId}/consumers[/{name}]` | Manage per-channel consumer URLs |
| `POST` | `/api/debug/fetch-transcript` | Operator backfill for `OfficialTranscriptFetcher` — body: `{meeting_id OR call_id, organizer_oid, meeting_chat_thread_id?}`. Idempotent on the supplied key; fires the same poller the auto-trigger uses (§7.12). |
| `POST` | `/api/debug/refresh-meeting-metadata` | Re-run the resolver chain (chat → joinWebUrl → onlineMeeting) and re-emit a metadata-complete `meeting.created`. Body: `{meeting_chat_thread_id}`. Fixes meetings emitted earlier with `subject:null`. |
| `GET` | `/api/debug/transcripts[/{sanitized_chat_thread_id}]` | Read the bot's NDJSON audit log under `C:/teams-bot-poc/meeting-logs/...`. List threads or tail one's `transcript|chat|system` stream. |

The bot's event fanout dispatcher (under `src/Services/`) POSTs every
non-throttled event to every registered consumer URL using the
`alfred-v2` envelope (`docs/event-contract.md`). In parallel,
`BlobEventArchive` writes the same envelope to Azure Blob Storage. The
Python sink is the built-in reference consumer; `server_v2.py` shows
the client-owned consumer shape.

### 8.2 Sink HTTP API (Container App)

Reference consumer for the `alfred-v2` contract.
Endpoints verified against `python/transcript_sink.py` route
decorators. Grouped; OpenAPI at `$SINK/openapi.json` is the canonical
spec.

**Ingress:**
- `POST /v2/events` — v2 envelope ingress, idempotent on `envelope_id`.
- `POST /events` — legacy v1 ingress kept for cutover only; new
  consumers use `/v2/events` or any URL path they own.

**v2 reads (`meeting_id`-keyed):**
- `GET /v2/meetings` (filters: `team_id`, `channel_id`, `since`, `until`)
- `GET /v2/meetings/{meeting_id}` and `/events`, `/transcript`
- `PATCH /v2/meetings/{meeting_id}` — operator-set subject
- `POST /v2/meetings/{meeting_id}/transcript-upload` — TXT/VTT upload
- `GET /v2/teams/{team_id}/channels/{channel_id}` and `/events`,
  `/threads/{thread_id}/messages`
- `GET /v2/resolve`, `GET /v2/index`

**Legacy meeting reads (current UI):** `GET /m`,
`/m/{tid}/{status,events,ledger,dossier}`, `POST /m/{tid}/{mute,end}`.
`/m/{tid}/events` is the SSE stream.

**Channel link + session helpers:** `GET /session/link/{tid}`,
`GET /channels/links`, `GET /c/{teamId}/{channelId}/events`,
`POST /session/{start,end,map-speaker,participants}`,
`GET /session/{status,analysis,events}`, `GET /session`.

**Session raw audit:** `GET /sessions`,
`GET /sessions/{sid}/{ledger,dossier,extractions,tool-calls,participants,speaker-identity,raw-events}`,
`GET /sessions/{sid}/raw-events/export.ndjson`,
`POST /sessions/{sid}/speaker-mapping`.

**Misc:** `/health`, `/stats`, `/product/spec`.

---

## 9. Data shape — what's in Postgres

DSN = `ALFRED_DB_URL` (prod: `pg-alfred-disney`, sourced from the
`alfred-db-url` Container App secret; §6.1.1). Schema in
`meeting_agent/persistence.py` via `psycopg[binary]` +
`psycopg_pool.ConnectionPool` (min=1, max=5, autocommit, dict_row).
Schema is portable from the previous sqlite layout — same table
names, same column names. The dialect deltas are: `?` placeholders
→ `%s`, `INSERT OR REPLACE` → `INSERT ... ON CONFLICT DO UPDATE`,
`PRAGMA table_info` → `information_schema.columns`. No behavioral
change for callers. Canonical tables:

```
sessions                    one row per meeting
meeting_events              normalized ledger (speech / chat / system)
raw_ingest_events           immutable per-event audit
raw_ingest_envelopes        immutable per-envelope archive (v2 ingress)
session_channel_links       chat_thread_id ↔ (team_id, channel_id, channel_thread_id)
meeting_participants        roster
participant_msi_bindings    MSI ↔ AAD
speaker_identity_links      speaker_N ↔ AAD (E3)
extractions                 one row per AlfredExtraction
tool_calls                  one row per agent tool invocation
dossier_items               latest decisions/questions/action_items/risks
meetings                    v2 meeting registry keyed on meeting_id (chat thread id)
transcript_uploads          operator-uploaded TXT/VTT keyed on meeting_id
```

Every event row carries optional `team_id`, `channel_id`,
`channel_thread_id`. Channel-wide rollup:
```sql
SELECT timestamp_utc, kind, source, text
FROM meeting_events
WHERE channel_id = '19:abc@thread.tacv2'
ORDER BY timestamp_utc ASC;
-- HTTP equivalent: GET /c/{teamId}/{channelId}/events
```

**`meeting_id` is the Teams chat thread id of the meeting**
(`19:meeting_<base64>@thread.v2`). It is the key the bot emits, the sink
persists, and every `/v2/meetings/{meeting_id}/...` endpoint accepts.
Graph's `onlineMeeting.id` is a separate identifier used internally only
when calling `/onlineMeetings/{id}/transcripts`; it is never written to
an envelope, blob path, or accepted as a sink key.

Schema migrations are additive in `_migrate`. Indexes on
newly-added columns must go in `_migrate`, NOT the main `SCHEMA`
string — the bootstrap statement batch runs before the migration adds
the column. Migration column-existence checks use
`information_schema.columns` (Postgres) rather than the old
`PRAGMA table_info` lookup.

---

## 10. Channel attachment — persistent listen+post

Two attach paths:

**A. Team install (preferred).** Install at team level via Teams Admin
Center → Manage apps → Install. The bot's `membersAdded` handler reads
`TeamsChannelData`, auto-attaches, and creates the Graph subscription.

**B. Operator API.**
```bash
curl -sS -X POST $BOT/api/channels/attach \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg tid "$TEAM_ID" --arg cid "$CHANNEL_ID" \
        '{team_id:$tid, channel_id:$cid, source:"manual_attach"}')"
```

Either path persists `(teamId, channelId)` to
`C:\teams-bot-poc\state\channel-attachments.json`, creates a Graph
change-notification subscription on
`teams/{teamId}/channels/{channelId}/messages`, re-issues on bot
restart, and POSTs every channel post to the sink as a
`conversation_kind:"channel"` chat event.

The bot's full state directory contents (all atomic temp+rename,
all reloaded by hosted services at startup):

| File | Owner | Purpose |
|---|---|---|
| `channel-attachments.json` | `ChannelAttachmentStore` | `(teamId, channelId)` + subscription state. Re-issues subscriptions on restart. |
| `conversation-references.json` | `FileBackedConversationReferenceStore` | Bot Framework `ConversationReference` per chat thread. Keeps `/api/send-chat` working across restarts. |
| `meeting-channel-links.json` | `MeetingChannelLinkStore` | `chat_thread_id → (team_id, channel_id)` overrides set via `@Alfred link to <channel-name>`. |
| `pending-transcript-fetches.json` | `OfficialTranscriptFetcher` | In-flight 30-min polls for post-meeting Microsoft transcripts. Restart resumes every record whose deadline hasn't truly expired (see §7.12). |

Hand-editable JSON in a pinch. Wipe the directory only as a last
resort: every store re-bootstraps from new events, but in-flight
transcript polls and the conversation-reference cache get lost.

Meetings spawned from an attached channel are a separate thread
(`19:meeting_<base64>@thread.v2`) — their own session in the sink. The
bot POSTs `/session/link` when channel context is first seen for the
meeting, binding the session and backfilling prior events. After
binding, `GET /c/{teamId}/{channelId}/events` returns channel posts
AND every meeting's events under one ordered timeline.

---

## 11. Editing rules — do not violate

1. **Single canonical meeting ledger.** `InterviewSession.meeting_events`,
   one session per `chat_thread_id`. Immutable layer:
   `raw_ingest_events`; back-link via `source_raw_event_ids`.
2. **`chat_thread_id` is THE session key.** Every transcript/chat must
   carry it; UI URL must require it (`/m/<chat_thread_id>`).
3. **`meeting_id` is the Teams chat thread id of the meeting**
   (`19:meeting_<base64>@thread.v2`). Graph's `onlineMeeting.id` is a
   separate identifier and is not used as the key. Never substitute a
   different surrogate. See §9.
4. **One inbound chat path.** `/api/messages` → Python `/chat`.
5. **Outbound through `send_to_meeting_chat`.** No parallel output. No
   `SEND/ASK/SILENT` enum on the extraction.
6. **`alfred.yaml` is the only source of truth** for prompt + policy.
   `AlfredAnalyzer` raises if `instructions` is missing — no code-side
   default.
7. **All persistent writes** go through `SessionStore`.
8. **Bot self-resolves TLS cert at startup.** Auto-renewal must remain
   transparent.
9. **One canonical implementation per concern.** No duplicate files,
   no `v2` copies (the polling-bridge sidecars are the only exception
   — §7.10), no override-with-fallback patterns.
10. **Fail fast** when prerequisites are unmet.
11. **Never glob/grep generated dirs** (`.venv`, `node_modules`,
    `bin/`, `obj/`, `__pycache__`, `*.egg-info`).
12. **`dotnet publish` always after `rm -rf bin obj`** (§7.4).
13. **Manifest changes**: bump `version`, regenerate the zip
    (`cd manifest && rm alfred-sandbox.zip && zip -j alfred-sandbox.zip
    manifest.json color.png outline.png`), re-import, possibly re-grant
    admin consent.

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
| PostgreSQL | `pg-alfred-disney` (`Standard_B1ms`, eastus2, db `alfred`, PG 16, 32 GB) — DSN in Container App secret `alfred-db-url`, env `ALFRED_DB_URL` |
| Sink file share (dormant) | `stalfreddisney/alfred-sink-data` — historical sqlite mount, no longer used |
| ACR | `acralfreddisneye02c0038.azurecr.io` |
| Azure OpenAI | `aoai-alfred-disney` (`gpt-5-mini`) |
| Speech Services | `speech-alfred-disney` (eastus) |

---

## 13. Common task recipes

**Add a column to `meeting_events`:**
1. Add to the `CREATE TABLE meeting_events` block in `persistence.py`
   (fresh DBs only).
2. Add to the `meeting_additive` list in `_migrate` (existing DBs).
3. Add the field to `MeetingEvent` in `models.py`.
4. Update `append_meeting_event` to write; update `get_ledger` (and
   `get_channel_ledger` if relevant) to SELECT.
5. New index → `CREATE INDEX` in `_migrate` only, NOT in `SCHEMA` (§9).
6. Point `ALFRED_DB_URL` at a throwaway Postgres database (drop +
   recreate to wipe), then `uv run pytest tests`. Test fixtures still
   pass a path-like argument for backwards-compatible naming, but the
   real persistence layer is Postgres via `psycopg`.

**Add a sink endpoint:** add the route in `transcript_sink.py`; use
`state["store"]` for SQL and `state["session_registry"]` for live
state; wrap long DB calls in `asyncio.to_thread(...)`; add a test in
`python/tests/test_sink.py` mirroring `TestChannelLinkAndRollup`.

**Modify the C# bot and deploy:** edit under `src/`; docker
compile-check (§3.3); commit + push to all three remotes (§4.1);
deploy via `deploy-azure-vm.sh` or §6.3 incremental; verify
`/api/calling/health` after restart and tail the service log (§7.7) —
"succeeded" alone is not enough.

**Channel-attached bot in a sandbox tenant:** re-import
`manifest/alfred-sandbox-v1.0.12.zip` in Teams Developer Portal →
M365 admin consents to RSC perms in Teams Admin Center → install at
team level → `curl $BOT/api/channels | jq` shows the attachment →
post in channel → `curl $SINK/c/{teamId}/{channelId}/events` shows it
within seconds.

---

## 14. Don't

- Don't create unsolicited markdown (`SUMMARY.md`, `final-guide.md`,
  `notes.md`, etc.). Updating `README.md`, `PROD.md`, or this file is
  fine when warranted.
- Don't trust this file blindly. `git log --oneline -20` on `main`
  to verify endpoints/scripts. `PROD.md` tracks deferred prod work.
