# LegionMeet (Alfred)

Teams meeting bot that auto-joins meetings, captures audio, transcribes it, and runs LLM analysis on the transcript. Two runtimes:

1. **C# media bot** on a Windows Server VM — joins calls via Microsoft Graph Communications, captures media, streams transcript events to the sink.
2. **Python FastAPI sink + React UI** on Azure Container Apps — receives transcripts, runs OpenAI Agents SDK analysis, renders a live "dossier" UI.

This README is the **operational source of truth**. If a runbook command in here is wrong, the deploy is wrong.

## System Architecture

```text
Teams Meeting
    ↓
TeamsMediaBot.exe (Windows VM, port 443/8445, public IP 172.190.7.169)
    │  • POST /api/calling/health  • POST /api/calling     • POST /api/messages
    │
    ├──► STT Provider (Deepgram or Azure Speech, runtime-configured)
    │
    └──► POST https://agent.qmachina.com/transcript
              ↓
         FastAPI sink (ca-alfred-api Container App)
              │  • / health  • /transcript  • /session/*  • /chat
              │
              ├──► Azure OpenAI (gpt-5-mini in aoai-alfred)
              │
              └──► SQLite + dossier state
                       ↓
                  React UI (ca-alfred-web Container App, served via nginx)
                  https://alfred.qmachina.com
```

## Production Topology (eastus)

| Component | Azure resource | Endpoint |
|---|---|---|
| Subscription | `70464868-52ea-435d-93a6-8002e83f0b89` | tenant `2843abed-8970-461e-a260-a59dc1398dbf` |
| Resource group | `rg-alfred-poc` (eastus) | — |
| Entra app | `Alfred` | appId `ff4b0902-5ae8-450b-bf45-7e2338292554` |
| Azure Bot | `alfred-bot-qmachina` | messaging `https://teamsbot.qmachina.com/api/messages`, calling `https://teamsbot.qmachina.com/api/calling` |
| Bot VM | `vm-alfred` (Standard_D4s_v3, Win2022 g2 Trusted Launch) | `172.190.7.169`, FQDN `vm-alfred-eastus.eastus.cloudapp.azure.com` |
| FastAPI sink | `ca-alfred-api` | `https://agent.qmachina.com` (custom) / `https://ca-alfred-api.orangecoast-aa65f885.eastus.azurecontainerapps.io` (default) |
| React UI | `ca-alfred-web` | `https://alfred.qmachina.com` (custom) / default CAE FQDN |
| Azure OpenAI | `aoai-alfred` | deployment `gpt-5-mini`, GlobalStandard, capacity 10 |
| Azure Speech | `speech-alfred` | S0, region eastus |
| ACR | `acralfredpoc70464868` | hosts `ca-alfred-api`, `ca-alfred-web` images |
| Container Apps env | `cae-alfred` | default domain `orangecoast-aa65f885.eastus.azurecontainerapps.io` |

## Repository Layout

| Path | Purpose |
|---|---|
| `src/` | C# Teams bot runtime + HTTP API |
| `python/` | FastAPI sink, React UI build inputs, analysis package, tests |
| `web/` | nginx config template that fronts the React UI Container App |
| `scripts/` | All deployment, bootstrap, and operational entrypoints |
| `manifest/` | Teams app manifest (`manifest.json` + icons; zipped for sideload) |
| `infra/` | Azure resource export (reference / drift detection only) |

## Where to Change What

| If you need to change… | Start here |
|---|---|
| Bot startup wiring | `src/Program.cs` |
| Bot HTTP API (`/api/calling/*`, `/api/messages`) | `src/Controllers/CallingController.cs` |
| Call join/leave/media handling | `src/Services/TeamsCallingBotService.cs` |
| Per-call audio buffering and forwarding | `src/Services/CallHandler.cs` |
| Transcript event contract (C#) | `src/Models/TranscriptEvent.cs` |
| Bot → sink forwarder | `src/Services/PythonTranscriptPublisher.cs` |
| Sink ingest endpoints + state flow | `python/transcript_sink.py` |
| LLM analysis behavior | `python/interview_agent/` |
| Product/spec validation | `python/legionmeet_platform/spec_loader.py` |
| Output route dispatch | `python/legionmeet_platform/routes/router.py` |
| React UI | `web/` |
| nginx /sink/ proxy behavior | `web/nginx.conf.template` |
| Teams app manifest fields | `manifest/manifest.json` |
| VM bootstrap (prereqs, config, build, service) | `scripts/bootstrap-production-vm.ps1` |
| VM cert issuance | `scripts/vm-request-letsencrypt-cert.ps1` |
| End-to-end VM deploy orchestration | `scripts/deploy-azure-vm.sh` |
| Python services deploy | `scripts/deploy-azure-agent.sh` |

## Deploy

### Prerequisites (one-time on dev machine)

```bash
# Azure CLI logged in to the Alfred tenant
az account show --query '{sub:id, tenant:tenantId, user:user.name}'
# Expected: sub 70464868-... tenant 2843abed-... user logan@qmachina.com

# Required secret files (read by scripts/deploy-azure-vm.sh)
test -s /tmp/app-secret.json    # {"appId":"ff4b0902-...","password":"..."}
test -s /tmp/vm-admin-pass.txt  # single-line VM azureuser password
test -s /tmp/speech-key.txt     # single-line Azure Speech key

# DNS pointing at the VM (must resolve to 172.190.7.169)
dig +short teamsbot.qmachina.com media.qmachina.com
```

### Bootstrap / re-bootstrap the bot VM

```bash
./scripts/deploy-azure-vm.sh
```

Idempotent. Runs five managed Run Command phases on `vm-alfred`:

| Phase | Script | What it does | Typical duration |
|---|---|---|---|
| 1/5 | `bootstrap-production-vm.ps1` | Installs prereqs (git, dotnet 8, nssm, choco, Server-Media-Foundation, VCRedist 140, OpenSSH), syncs repo, writes `appsettings.production.json`, runs `dotnet publish` | 5–10 min on first run, ~2 min on re-run |
| 2/5 | `vm-open-firewall.ps1` | Opens Windows Firewall 80/443/8445 | <1 min |
| 3/5 | `vm-install-win-acme.ps1` | Installs win-acme via Chocolatey | 2–3 min |
| 4/5 | `vm-request-letsencrypt-cert.ps1` | HTTP-01 issues TLS cert for `teamsbot.qmachina.com,media.qmachina.com`, registers Task Scheduler renewal | 2–3 min |
| 5/5 | `vm-finalize-bootstrap.ps1` | Writes cert thumbprint into config, installs/updates `TeamsMediaBot` Windows service via nssm, starts it | 1–2 min |

Preflight inside `deploy-azure-vm.sh` also enables boot diagnostics, ensures NSG recovery rules (22, 5986), and probes the guest agent via Run Command if the instance view shows null (ARM cache lag). See `scripts/deploy-azure-vm.sh`.

### Deploy / redeploy the Python sink + React UI

```bash
./scripts/deploy-azure-agent.sh
```

Builds Docker images via ACR (`acralfredpoc70464868`), updates Container Apps `ca-alfred-api` and `ca-alfred-web`, attaches/binds custom domains.

### Local development

```bash
cd python && uv sync

# Terminal 1: sink
uv run python run_variant_sink.py \
  --instance dev --port 8765 \
  --product-spec legionmeet_platform/specs/alfred.yaml

# Terminal 2: UI
uv run python run_variant_ui.py \
  --instance dev --port 8501 \
  --sink-url http://127.0.0.1:8765 \
  --product-spec legionmeet_platform/specs/alfred.yaml

# Tests
uv run pytest tests -v
```

C# build:

```bash
cd src && dotnet restore && dotnet build --configuration Release
```

## Operate

All on-VM operations go through **managed Run Command** (`az vm run-command create`). **Never** use `az vm run-command invoke` — see Troubleshoot.

### Restart the bot service

```bash
az vm run-command create -g rg-alfred-poc --vm-name vm-alfred \
  --run-command-name restart-bot --location eastus \
  --script 'Restart-Service TeamsMediaBot -Force; Start-Sleep -Seconds 8; Get-Service TeamsMediaBot' \
  --async-execution false --timeout-in-seconds 60
az vm run-command delete -g rg-alfred-poc --vm-name vm-alfred --run-command-name restart-bot --yes
```

### Tail bot logs

```bash
az vm run-command create -g rg-alfred-poc --vm-name vm-alfred \
  --run-command-name tail-logs --location eastus \
  --script 'Get-Content C:\teams-bot-poc\logs\service-output.log -Tail 80; Write-Host "---STDERR---"; Get-Content C:\teams-bot-poc\logs\service-error.log -Tail 40 -ErrorAction SilentlyContinue' \
  --async-execution false --timeout-in-seconds 60
az vm run-command show -g rg-alfred-poc --vm-name vm-alfred --run-command-name tail-logs --instance-view --query "instanceView.output" -o tsv
az vm run-command delete -g rg-alfred-poc --vm-name vm-alfred --run-command-name tail-logs --yes
```

### Push a code change to production

```bash
git push origin feat/alfred-chat-modality   # or whichever branch is deployed
./scripts/deploy-azure-vm.sh                 # re-bootstraps in-place; pulls + republishes + restarts service
```

### Force a TLS cert renewal

The win-acme scheduled task auto-renews. To force now:

```bash
az vm run-command create -g rg-alfred-poc --vm-name vm-alfred \
  --run-command-name force-renew --location eastus \
  --script 'Start-ScheduledTask -TaskName "win-acme renew (acme-v02.api.letsencrypt.org)"' \
  --async-execution false --timeout-in-seconds 60
```

## Check

### Public health (run from anywhere)

```bash
curl -sS -m 10 https://teamsbot.qmachina.com/api/calling/health
# → {"status":"Healthy","timestampUtc":"...","service":"Alfred","activeCalls":0}

curl -sS -m 10 https://agent.qmachina.com/health
# → {"status":"healthy","variant_id":"alfred","product_id":"alfred"}

curl -sS -m 10 -o /dev/null -w "%{http_code}\n" https://alfred.qmachina.com
# → 200
```

### Service + cert state on the VM

```bash
az vm run-command create -g rg-alfred-poc --vm-name vm-alfred \
  --run-command-name status --location eastus \
  --script @- --async-execution false --timeout-in-seconds 60 <<'EOF'
Get-Service TeamsMediaBot | Format-List Name, Status, StartType
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in @(443,8445) } | Format-Table LocalAddress, LocalPort, OwningProcess
Get-ChildItem Cert:\LocalMachine\My | Where-Object { $_.Subject -like "*qmachina*" } | Format-List Subject, Thumbprint, NotAfter
EOF
az vm run-command show -g rg-alfred-poc --vm-name vm-alfred --run-command-name status --instance-view --query "instanceView.output" -o tsv
az vm run-command delete -g rg-alfred-poc --vm-name vm-alfred --run-command-name status --yes
```

### Recent VM control-plane operations

```bash
az monitor activity-log list \
  --resource-id "/subscriptions/70464868-52ea-435d-93a6-8002e83f0b89/resourceGroups/rg-alfred-poc/providers/Microsoft.Compute/virtualMachines/vm-alfred" \
  --start-time "$(date -u -v-1H '+%Y-%m-%dT%H:%M:%SZ')" \
  --query "[].{time:eventTimestamp, op:operationName.localizedValue, status:status.localizedValue}" \
  -o table
```

## Troubleshoot

| Symptom | Root cause | Fix |
|---|---|---|
| `az vm get-instance-view` returns `vmAgent: null` | ARM instance-view cache lag (5–10 min). Agent may already be healthy. | Probe with a synchronous Run Command (`Write-Host alive`). If it returns Succeeded in <60s, agent is fine — ignore the null. |
| `provisioningState: Updating` indefinitely (>10 min) and probe also fails | Wedged action Run Command extension left orphaned plugin state | RDP to VM (port 3389, password in `/tmp/vm-admin-pass.txt`). `Remove-Item -Recurse -Force 'C:\Packages\Plugins\Microsoft.CPlat.Core.RunCommandWindows*'`; `Remove-Item 'C:\WindowsAzure\Logs\AggregateStatus\aggregatestatus.json'`; `net stop WindowsAzureGuestAgent; net stop RdAgent; net start RdAgent; net start WindowsAzureGuestAgent`. Wait 3 min. |
| `Conflict: Run command extension execution is in progress` on any `az vm` op | Legacy action Run Command (`invoke`) wedged the extension | Switch caller to **managed** Run Command (`az vm run-command create --run-command-name ...`). Never use `az vm run-command invoke` for anything in this repo. |
| Bot service Running but health endpoint times out | Bot is in a crashloop. Check `C:\teams-bot-poc\logs\service-error.log`. | Most common: `DllNotFoundException: NativeMedia` — Server-Media-Foundation Windows feature or VC++ Redistributable missing. Re-run `./scripts/deploy-azure-vm.sh` (Phase 1's `Install-MediaPlatformPrereqs` installs both). |
| `dotnet publish` succeeds but service crashes immediately | Likely missing native dep (`NativeMedia.dll` needs `mfplat.dll`, `mf.dll`, `msvcp140.dll`, `vcruntime140.dll`) | Same as above — `./scripts/deploy-azure-vm.sh` ensures all four. |
| HTTP-01 cert request fails | Port 80 not reachable from public internet, or DNS not pointing at VM | Verify `dig +short teamsbot.qmachina.com` returns `172.190.7.169`. Verify NSG rule `AllowHTTP` priority 1000 exists. Verify `Get-NetFirewallRule -DisplayGroup "World Wide Web Services (HTTP Traffic-In)"` is enabled on the VM. |
| nginx `/sink/` proxy returns 502 | Missing SNI when fronting Container Apps HTTPS ingress | `web/nginx.conf.template` already sets `proxy_ssl_server_name on;` and `proxy_set_header Host $proxy_host;`. If you regenerate the template, keep both. |
| Bot doesn't auto-join meeting after invite | Azure Bot Service registration missing or Graph permissions not consented | Verify `az bot show -g rg-alfred-poc -n alfred-bot-qmachina`. Verify `az ad app permission list --id ff4b0902-5ae8-450b-bf45-7e2338292554` shows admin-consented `Calls.AccessMedia.All` + `Calls.JoinGroupCall.All`. |

For the underlying-incident analysis behind each row, see the memory entries under `~/.claude/projects/-Users-logan-robbins-research-teams-bot-poc/memory/feedback_*.md` (each row's fix maps to one memory).

## Configuration

### Production runtime config

Lives at `C:\teams-bot-poc\src\Config\appsettings.production.json` on the VM. Generated by `bootstrap-production-vm.ps1`. **Do not edit by hand** — re-run the deploy script if a value needs to change.

Key knobs (all set automatically from environment files + Azure resources):

| Section.Key | Source | Purpose |
|---|---|---|
| `Bot.AppId` / `Bot.AppSecret` / `Bot.TenantId` | `/tmp/app-secret.json` | Entra app credentials |
| `Bot.NotificationUrl` | `https://teamsbot.qmachina.com/api/calling` | Where Teams sends incoming-call webhooks |
| `MediaPlatformSettings.CertificateThumbprint` | Generated by Phase 5 from issued LE cert | TLS for Graph media SDK |
| `MediaPlatformSettings.ServiceFqdn` | `media.qmachina.com` | Public FQDN for media endpoint |
| `MediaPlatformSettings.InstancePublicIPAddress` | VM public IP `172.190.7.169` | NAT awareness for media SDK |
| `Stt.Provider` | `AzureSpeech` (override via `STT_PROVIDER` env to deploy script) | STT backend selector |
| `Stt.AzureSpeech.Key` / `Stt.AzureSpeech.Region` | `/tmp/speech-key.txt`, `eastus` | Azure Speech credentials |
| `TranscriptSink.PythonEndpoint` | Auto-resolved from `ca-alfred-api` Container App FQDN + `/transcript` | Bot → sink target |
| `JoinMode.PreferredMode` | Default `invite_and_graph_join` | See `docs/TEAMS-AUTO-INVITE-SETUP.md` for `policy_auto_invite` |

### Bot HTTP endpoints

```bash
# Health
curl https://teamsbot.qmachina.com/api/calling/health

# Explicit join (Graph join after bot is invited)
curl -X POST https://teamsbot.qmachina.com/api/calling/join \
  -H "Content-Type: application/json" \
  -d '{
    "joinUrl":"<teams-meeting-join-url>",
    "displayName":"Alfred",
    "meetingId":"<external-meeting-id>",
    "organizerTenantId":"2843abed-8970-461e-a260-a59dc1398dbf",
    "botAttendeePresent":true,
    "joinMode":"invite_and_graph_join"
  }'
```

Response codes: `200` started, `202` deferred (policy mode), `400` `BOT_NOT_INVITED`, `403` permission/tenant issue, `502` `CALL_JOIN_FAILED_7504_OR_7505`.

One-shot operator wrapper:

```bash
./scripts/join_meeting.sh "<teams-meeting-join-url>" "<candidate-name>"
JOIN_DRY_RUN=1 ./scripts/join_meeting.sh ...   # prints the curl, doesn't send
```

## Maintenance Rule

When an entry path, canonical command, deploy step, or troubleshoot fix changes, update this README in the same change set. This file is the operational source of truth.
