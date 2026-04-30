# Alfred

Microsoft Teams meeting assistant. Joins meetings, captures audio, transcribes per-speaker, ingests meeting chat, runs an LLM agent that produces a live "dossier" (decisions, open questions, action items, risks), and posts back into the meeting chat. Read `ALFRED.md` first if you're modifying behavior.

## Live environments

| | qMachina (prod) | Disney sandbox |
|---|---|---|
| Azure subscription | `70464868-52ea-435d-93a6-8002e83f0b89` | `e02c0038-82c8-4655-9647-38083f301099` |
| Azure tenant | `2843abed-...` (qmachina) | `56b731a8-...` (disney.com) |
| M365 tenant (Teams + Entra app) | same as Azure tenant | `38387f0b-...` (plutosdoghouse.com) |
| Resource group | `rg-alfred-poc` | `rg-alfred-disney` |
| Bot VM | `vm-alfred` (`172.190.7.169`, `teamsbot.qmachina.com`) | `vm-alfred-disney` (`alfred-disney-bot.eastus.cloudapp.azure.com`) |
| Azure Bot Service | `alfred-bot-qmachina` | `bot-alfred-disney` (SingleTenant, app tenant Disney M365) |
| Bot AppId | `ff4b0902-5ae8-450b-bf45-7e2338292554` | `207a38a4-67c5-4ef9-ada8-ea7998734d59` |
| Sink (Container App) | `https://agent.qmachina.com` | `https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io` |
| UI (Container App) | `https://alfred.qmachina.com` | `https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io` |
| Azure OpenAI | `aoai-alfred` (`gpt-5-mini`, GlobalStandard cap 10) | `aoai-alfred-disney` (same) |
| Manifest zip | `manifest/alfred.zip` | `manifest/disney/alfred-sandbox.zip` |

## Repos and deploy key

| | URL | Visibility | Role |
|---|---|---|---|
| Source of truth | `git@github.com:logan-robbins/alfred-teams-bot.git` (`main`) | **PRIVATE** | All Alfred work lives here. Both VMs clone from here. Local `main` tracks `private/main`. |
| Public reference | `git@github.com:logan-robbins/teams-bot-poc.git` (`main`) | PUBLIC | Original public README + Bicep export. Frozen — no Alfred deploy work goes here. |

`git push` from a fresh checkout pushes to `private/main`. To set this up:

```bash
git clone git@github.com:logan-robbins/alfred-teams-bot.git
cd alfred-teams-bot
# (optional) keep the public repo around for occasional cherry-picks back:
git remote add origin https://github.com/logan-robbins/teams-bot-poc.git
git remote rename origin public          # avoid the name collision
git remote rename origin-name private    # if needed, ensure private is the default `origin`
```

**Deploy key (`/tmp/alfred-deploy-key`)** — VMs clone the private repo over SSH using a read-only ed25519 deploy key registered on the repo. Both `deploy-azure-vm.sh` runs require it.

```bash
# Generate (one-time per dev machine — the public half is already registered)
ssh-keygen -t ed25519 -C "alfred-teams-bot deploy" -f /tmp/alfred-deploy-key -N ""
chmod 600 /tmp/alfred-deploy-key

# Register the public half on the private repo (read-only)
gh api repos/logan-robbins/alfred-teams-bot/keys \
  -f title="$(hostname)-$(date -u +%Y-%m-%d)" \
  -f key="$(cat /tmp/alfred-deploy-key.pub)" \
  -F read_only=true
```

The bootstrap drops the key on each VM at `C:\ProgramData\alfred\deploy_key` with ACL `SYSTEM + azureuser : Read` and points `GIT_SSH_COMMAND` at it. Keys are per-repo, so the same key works for both VMs.

## Start (install Alfred in a tenant)

1. Upload the manifest zip via **Teams Developer Portal** (`https://dev.teams.microsoft.com/apps`) → **Import app**.
2. Click **Preview in Teams** → **Add to a chat** (or to a meeting). The install grants the 5 chat-scoped Application RSC perms (`ChatMessage.Read.Chat`, `ChatMessageReadReceipt.Read.Chat`, `OnlineMeetingParticipant.Read.Chat`, `Calls.AccessMedia.Chat`, `Calls.JoinGroupCalls.Chat`). No tenant-wide Graph perms are requested.
3. Start a session, then DM the bot or invite it to a meeting. Trigger an explicit join with:
   ```bash
   ./scripts/join_meeting.sh "<teams-meeting-join-url>" "Alfred"
   ```

If user-consent is gated by an "unverified publisher" tenant policy, an M365 Global Administrator must grant org-wide consent for the app in **Teams Admin Center → Manage apps → Permissions tab**. Only an admin can do this; the manifest can't bypass it.

## Operate

All commands assume the right `az account set --subscription <sub>` for the target environment. Replace `<rg>` / `<vm>` / `<sink-fqdn>` per the environment table above.

```bash
# Public health (run from anywhere)
curl -sS -m 10 https://teamsbot.qmachina.com/api/calling/health
curl -sS -m 10 https://agent.qmachina.com/health
curl -sS -m 10 -o /dev/null -w "%{http_code}\n" https://alfred.qmachina.com

# Sink stats (events_received, session, agent_analyses, etc.)
curl -sS https://<sink-fqdn>/stats | jq

# Restart bot service on the VM
az vm run-command create -g <rg> --vm-name <vm> --location eastus \
  --run-command-name restart-bot \
  --script 'Restart-Service TeamsMediaBot -Force; Start-Sleep -Seconds 8; Get-Service TeamsMediaBot' \
  --async-execution false --timeout-in-seconds 60
az vm run-command delete -g <rg> --vm-name <vm> --run-command-name restart-bot --yes

# Tail bot logs (stdout + stderr)
az vm run-command create -g <rg> --vm-name <vm> --location eastus \
  --run-command-name tail-logs \
  --script 'Get-Content C:\teams-bot-poc\logs\service-output.log -Tail 80; Write-Host "---STDERR---"; Get-Content C:\teams-bot-poc\logs\service-error.log -Tail 40 -ErrorAction SilentlyContinue' \
  --async-execution false --timeout-in-seconds 60
az vm run-command show -g <rg> --vm-name <vm> --run-command-name tail-logs --instance-view --query "instanceView.output" -o tsv
az vm run-command delete -g <rg> --vm-name <vm> --run-command-name tail-logs --yes
```

**Always use `az vm run-command create`. Never `az vm run-command invoke`** — the legacy action variant wedges the extension and forces a manual `Microsoft.CPlat.Core.RunCommandWindows*` cleanup over RDP.

## Push a code change

```bash
git push                                  # local main → private/main

# qMachina prod
./scripts/deploy-azure-vm.sh              # uses defaults; reads /tmp/alfred-deploy-key
./scripts/deploy-azure-agent.sh           # rebuilds + redeploys sink/UI

# Disney sandbox (env overrides)
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
  SKIP_REPO_SYNC=1 \
  ./scripts/deploy-azure-vm.sh
```

`SKIP_REPO_SYNC` controls whether the bootstrap pulls fresh code:
- **`SKIP_REPO_SYNC=0`** — required the **first** time a VM points at the private repo (clones, sets origin URL, normalizes the fetch refspec, pulls latest). Also use after pushing new commits.
- **`SKIP_REPO_SYNC=1`** — fast path; uses whatever's already on disk. Use for config-only changes (env vars, secrets) where source code didn't change.

The script stops the bot service before publishing so the running DLL doesn't lock the new build. If you ever see "file in use" during publish, the service wasn't stopped — re-run.

### Migrating an existing VM from the old public repo

The qMachina `vm-alfred` was originally bootstrapped against the now-empty public branch. The first deploy after this migration must include `DEPLOY_KEY_FILE=/tmp/alfred-deploy-key` and `SKIP_REPO_SYNC=0` — the bootstrap will:
1. Detect the stale `remote.origin.url` (`https://github.com/logan-robbins/teams-bot-poc.git`) and switch it to the private SSH URL.
2. Replace the single-branch fetch refspec with `+refs/heads/*:refs/remotes/origin/*` (the old `--single-branch` clone left a refspec that silently swallows new branches).
3. Install the deploy key, fetch, force-checkout `main`, and rebuild.

Subsequent deploys behave normally.

## Debug

| Symptom | Fix |
|---|---|
| `/api/calling/health` Healthy but Teams calls don't join | Microsoft RTM media allowlist required for the bot AppId. Submit at `https://aka.ms/teams-rtm-onboarding` (~2 weeks). Until approved, bot joins the call but the media SDK fails to attach to audio. |
| Bot service Running but stderr shows `MediaPlatform needs at least 2 cores` | VM has <2 physical cores. Resize to `Standard_D4s_v3` (or larger): `az vm resize -g <rg> -n <vm> --size Standard_D4s_v3`, then restart bot service. |
| Bot service Running but stderr shows `DllNotFoundException: NativeMedia` | Server-Media-Foundation feature or VC++ Redistributable missing. Re-run `./scripts/deploy-azure-vm.sh`; Phase 1 reinstalls both. |
| `POST /api/messages` returns 401 `Invalid AppId passed on token` | `appsettings.production.json` is missing `MicrosoftAppId/Password/Type/TenantId` at the **root** (Bot Framework reads these, distinct from `Bot.*`). Re-run `./scripts/deploy-azure-vm.sh` (the bootstrap script writes them now). |
| `POST /api/messages` returns 500 `CloudAdapter ambiguous constructors` | DI registration regressed. The factory in `src/Program.cs` must explicitly select `CloudAdapter(BotFrameworkAuthentication, ILogger)`. |
| Sink `events_received` increments but `session_events: 0` and no `[CHAT]` log line | No active session. The sink only persists chat to SQLite when a session is active. `POST /session/start` first. |
| `az containerapp` op returns 403 + RBAC role-assignment error during create | Use ACR admin credentials inline: `az containerapp registry set --server <acr>.azurecr.io --username <u> --password <p>` after create. |
| ACR build fails on `nginx:alpine`/`node:20-alpine` with `429 toomanyrequests` | Pre-import into ACR: `az acr import --name <acr> --source docker.io/library/nginx:alpine --image nginx:alpine`. Use `web/Dockerfile.disney-acrcache` which references the local mirror. |
| `git fetch origin main` on the VM returns exit 0 but `origin/main` doesn't move | Stale single-branch refspec from the original `git clone --branch X --single-branch`. Run `git config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'` inside the repo, then `git fetch --prune origin`. The bootstrap now does this automatically when `remote.origin.url` changes. |
| Bootstrap aborts with `REPO_URL='git@...' is SSH but DEPLOY_KEY_FILE='...' is empty` | The deploy script needs the SSH deploy key. See **Repos and deploy key** above for how to generate + register one. |
| `az vm get-instance-view` returns `vmAgent: null` | ARM cache lag. Probe directly with a Run Command (`Write-Host alive`); if it returns Succeeded the agent is fine. |
| `Conflict: Run command extension execution is in progress` | Legacy `az vm run-command invoke` wedged the extension. RDP in, remove `C:\Packages\Plugins\Microsoft.CPlat.Core.RunCommandWindows*`, restart `WindowsAzureGuestAgent` + `RdAgent`. |

## Deploy a new tenant

Prerequisite: target Azure subscription accessible via `az login`, plus an Entra app in the target M365 tenant with a client secret. The Entra app must be `AzureADMultipleOrgs` (Teams calling bot requirement) with an `identifierUri` of `api://botid-<appId>`.

The full sequence is in `PLAN-disney-deploy.md`. Summary:

```bash
# 1. Foundation (RG, AOAI, Speech, ACR, CAE)
az group create -n rg-alfred-<env> -l eastus
az cognitiveservices account create -n aoai-alfred-<env> -g rg-alfred-<env> -l eastus --kind OpenAI --sku S0 --custom-domain aoai-alfred-<env>
az cognitiveservices account deployment create -n aoai-alfred-<env> -g rg-alfred-<env> --deployment-name gpt-5-mini --model-name gpt-5-mini --model-version 2025-08-07 --model-format OpenAI --sku-capacity 10 --sku-name GlobalStandard
az cognitiveservices account create -n speech-alfred-<env> -g rg-alfred-<env> -l eastus --kind SpeechServices --sku S0
az acr create -n acralfred<env><sub-suffix> -g rg-alfred-<env> -l eastus --sku Standard --admin-enabled true
az containerapp env create -n cae-alfred-<env> -g rg-alfred-<env> -l eastus

# 2. Container Apps for sink + UI (build images via ACR, then create apps)
az acr build --registry acralfred<env><sub-suffix> --image ca-alfred-api:1 --file python/Dockerfile python/
az acr build --registry acralfred<env><sub-suffix> --image ca-alfred-web:1 --file web/Dockerfile web/

# 3. Azure Bot Service (SingleTenant, MicrosoftAppTenantId = home tenant of the Entra app)
az bot create -g rg-alfred-<env> -n bot-alfred-<env> \
  --app-type SingleTenant --appid <appId> --tenant-id <m365-tenantId> \
  --endpoint "https://<vm-fqdn>/api/messages" --location global --sku F0
az bot msteams create -g rg-alfred-<env> -n bot-alfred-<env> \
  --enable-calling true --calling-web-hook "https://<vm-fqdn>/api/calling"

# 4. VM (Standard_D4s_v3 minimum — Graph media SDK requires ≥2 physical cores)
az network public-ip create -g rg-alfred-<env> -n pip-alfred-<env> -l eastus \
  --sku Standard --allocation-method Static --dns-name alfred-<env>-bot
# NSG with 22, 80, 443, 3389, 5986, 8445
az vm create -g rg-alfred-<env> -n vm-alfred-<env> -l eastus \
  --image MicrosoftWindowsServer:WindowsServer:2022-datacenter-azure-edition:latest \
  --size Standard_D4s_v3 --admin-username azureuser --admin-password "<pwd>" \
  --public-ip-address pip-alfred-<env> --nsg <nsg> \
  --security-type TrustedLaunch --enable-secure-boot true --enable-vtpm true \
  --computer-name alfred-<env>-bot   # 15-char Windows limit

# 5. Bootstrap the VM (parameterized; SKIP_REPO_SYNC=0 first time)
RG_NAME=rg-alfred-<env> VM_NAME=vm-alfred-<env> \
  TENANT_ID=<m365-tenantId> APP_SECRET_FILE=/tmp/<env>-app-secret.json \
  VM_ADMIN_PASS_FILE=/tmp/<env>-vm-admin-pass.txt SPEECH_KEY_FILE=/tmp/<env>-speech-key.txt \
  BOT_HOSTNAME=<vm-fqdn> MEDIA_HOSTNAME=<vm-fqdn> \
  CERT_FRIENDLY_NAME=<env>-cert CERT_EMAIL=<email> \
  STT_PROVIDER=AzureSpeech AZURE_SPEECH_REGION=eastus \
  SKIP_REPO_SYNC=0 ./scripts/deploy-azure-vm.sh
```

Generate the manifest from `manifest/disney/manifest.json` as a template — replace the Entra app id, name, branding, then `zip -j alfred-<env>.zip manifest.json color.png outline.png`.

## Local dev

```bash
cd python && uv sync
uv run python run_variant_sink.py --instance dev --port 8765 --product-spec legionmeet_platform/specs/alfred.yaml
# UI: cd web && npm install && npm run dev
# Tests: cd python && uv run pytest tests -v
# C# build: cd src && dotnet restore && dotnet build --configuration Release
```

---

## Next Steps

### 1. Persistent sink storage

The sink's SQLite (`/app/output/alfred/alfred.sqlite3`) lives in the container's ephemeral filesystem. Any Container App revision rollout (env var change, image bump, scale config) destroys it along with the in-memory session. Two-part fix:

- **Mount Azure Files at `/app/output/`** so SQLite survives rollouts.
  ```bash
  STORAGE=stalfred<env><suffix>
  SHARE=alfred-state
  az storage account create -n $STORAGE -g rg-alfred-<env> -l eastus --sku Standard_LRS
  az storage share-rm create --resource-group rg-alfred-<env> --storage-account $STORAGE --name $SHARE --quota 5
  KEY=$(az storage account keys list -g rg-alfred-<env> -n $STORAGE --query "[0].value" -o tsv)
  az containerapp env storage set -g rg-alfred-<env> -n cae-alfred-<env> --storage-name alfred-files \
    --azure-file-account-name $STORAGE --azure-file-account-key "$KEY" --azure-file-share-name $SHARE \
    --access-mode ReadWrite
  # Re-create ca-alfred-api YAML with volume mount /app/output → alfred-files
  ```
- **Auto-resume the active session on sink startup** (small change in `python/transcript_sink.py` to query the most recent open session from SQLite and re-activate it). Without this, even with persistent SQLite the in-memory `session_manager.is_active` flag starts False after every restart.

### 2. Test and retrieve a sink transcript

Verifying end-to-end without a real meeting:

```bash
SINK=https://<sink-fqdn>

# Start a session
curl -sS -X POST $SINK/session/start -H "Content-Type: application/json" \
  -d '{"meeting_url":"https://teams.microsoft.com/test","candidate_name":"Test","instance_id":"alfred"}'
# → returns session_id

# Trigger inputs:
#   - DM the bot in Teams (Disney M365 user → bot)  → arrives at /chat
#   - Or POST a synthetic transcript event:
curl -sS -X POST $SINK/transcript -H "Content-Type: application/json" \
  -d '{"session_id":"<id>","event_type":"final","text":"hello world","speaker":"alice","timestamp_utc":"2026-04-30T17:00:00Z"}'

# Watch live (SSE)
curl -N $SINK/session/events

# Snapshot the current session
curl -sS $SINK/session/status | jq

# Pull the persisted ledger / dossier / extractions
curl -sS $SINK/sessions/<id>/ledger      | jq
curl -sS $SINK/sessions/<id>/dossier     | jq
curl -sS $SINK/sessions/<id>/extractions | jq
curl -sS $SINK/sessions/<id>/tool-calls  | jq
```

For full payload schemas, see `python/transcript_sink.py` and `python/meeting_agent/models.py`.

### 3. RTM media allowlist for new bot identities

Each new bot AppId needs Microsoft to allowlist it for real-time media before `Calls.AccessMedia.Chat` actually attaches to audio. Submit at `https://aka.ms/teams-rtm-onboarding` after the bot registration is in place. Approval typically takes ~2 weeks. Until then: chat I/O works, audio capture doesn't.

### 4. Replace deprecated multi-tenant Bot Service

Microsoft deprecated multi-tenant Azure Bot Service creation in July 2025. New deploys use SingleTenant (set `MicrosoftAppTenantId` to the Entra app's home tenant). Existing multi-tenant bots still function but should migrate to SingleTenant or User-Assigned Managed Identity at next major change.
