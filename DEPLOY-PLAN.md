# teams-bot-poc — westus redeploy + auto-accept fix plan

**Date:** 2026-04-20
**Subscription:** `Azure subscription 1` (`70464868-52ea-435d-93a6-8002e83f0b89`)
**Tenant:** `2843abed-8970-461e-a260-a59dc1398dbf`
**User:** logan@qmachina.com
**Target region:** westus (all new resources)

## Current-state inventory

- **App Registration** `TeamsMediaBotPOC` (`ff4b0902-5ae8-450b-bf45-7e2338292554`) exists. Has Graph app permissions `Calls.AccessMedia.All` + `Calls.JoinGroupCall.All`. Admin consent status TBD.
- **No Azure Bot Service** anywhere in subscription — root cause of the "auto-accept doesn't work" symptom (Teams has no route to deliver incoming-call webhooks to the bot).
- **Old RG** `rg-teams-media-bot-poc` (eastus) contains only a leftover `vault832` Recovery Services vault. Everything else torn down.
- **DNS (GoDaddy, live)**:
  - `agent.qmachina.com` → CNAME `ca-talestral-api.grayglacier-84d7709e.eastus.azurecontainerapps.io` (NXDOMAIN — stale)
  - `interview.qmachina.com` → CNAME `ca-talestral-ui.grayglacier-84d7709e.eastus.azurecontainerapps.io` (NXDOMAIN — stale)
  - `teamsbot.qmachina.com` → A `52.188.117.153` (unreachable — stale)
  - `media.qmachina.com` → A `52.188.117.153` (unreachable — stale)
- **westus quotas**: 10 vCPU for `Standard_DSv3` family — sufficient for one `Standard_D4s_v3` (4 vCPU).

## End state

Inviting the Talestral bot to a Teams meeting causes it to auto-join, capture audio, stream transcripts to the Python sink at `agent.qmachina.com`, and render live analysis at `interview.qmachina.com`.

---

## Phase 0 — cleanup (5 min)

- Leave `rg-teams-media-bot-poc` / `vault832` alone (empty vault is free; deletion is slow due to soft-delete).
- Confirm no other leftover resources.

## Phase 1 — Python agent + Azure OpenAI in westus (~20 min)

1. `az group create -n rg-teams-bot-westus -l westus`
2. Create Azure OpenAI `aoai-talestral-westus` (S0) in westus. If gpt-5-mini capacity is missing there, place AOAI in `westus3` or `eastus` and cross-region-call from ACA.
3. Deploy `gpt-5-mini` model version `2025-08-07`, SKU `GlobalStandard`, capacity 10.
4. Create Container Apps env `cae-talestral-westus` (Consumption workload profile, Log Analytics auto-create).
5. `az containerapp create --source python/ --ingress external --target-port 8765` → `ca-talestral-api`, env vars:
   - `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_KEY`, `AZURE_OPENAI_DEPLOYMENT=gpt-5-mini`, `OPENAI_API_TYPE=azure`, `OPENAI_REASONING_EFFORT=low`
   - `PRODUCT_SPEC_PATH=/app/legionmeet_platform/specs/talestral.json`, `VARIANT_ID=default`, `INSTANCE_ID=prod`
6. `az containerapp create --source python/ --dockerfile Dockerfile.streamlit --ingress external --target-port 8501` → `ca-talestral-ui`, env vars:
   - `SINK_URL=https://<api-fqdn>`
7. Capture new env default-domain (format: `<word>-<hex>.westus.azurecontainerapps.io`).
8. **Manual (user)**: Update GoDaddy DNS:
   - `agent` CNAME → `ca-talestral-api.<new-env-domain>`
   - `interview` CNAME → `ca-talestral-ui.<new-env-domain>`
9. Wait for propagation (typically 1–5 min on GoDaddy).
10. `az containerapp hostname add` + `az containerapp hostname bind --validation-method CNAME` to attach custom domains with managed certs.
11. Verify: `curl https://agent.qmachina.com/health` → `200 OK`.

## Phase 2 — fix C# auto-accept (10 min code)

**File:** `src/Services/TeamsCallingBotService.cs`

1. **Line ~221**: change `await call.AnswerAsync(mediaSession).ConfigureAwait(false);` to `await call.AnswerAsync(mediaSession, new[] { Modality.Audio }).ConfigureAwait(false);`. Microsoft's incoming-call sample passes an explicit modality array — without it, audio-only bots can fail to transition out of `Establishing` state.
2. Verify `CallHandler.cs` subscribes to audio-socket events **before** the call reaches `Established` state and starts the transcriber on `CallStateChanged -> Established` (not in the constructor). Adjust if needed.
3. Optional: add `acceptedModalities` to the `JoinMeetingAsync` path as well for consistency.

These are preemptive fixes based on reading the code; real symptoms will show up in Serilog once Phase 4 is live. Be prepared to iterate.

## Phase 3 — Azure Bot Service registration (10 min)

**This is the real fix** — without an Azure Bot registration, Teams never sends incoming-call webhooks.

1. `az bot create --resource-group rg-teams-bot-westus --name talestral-bot --kind registration --app-type MultiTenant --appid ff4b0902-5ae8-450b-bf45-7e2338292554 --endpoint https://teamsbot.qmachina.com/api/messages --sku F0 --location global`
2. Enable Microsoft Teams channel.
3. Configure calling webhook: `https://teamsbot.qmachina.com/api/calling`.
4. Grant admin consent on the App Registration's Graph permissions:
   `az ad app permission admin-consent --id ff4b0902-5ae8-450b-bf45-7e2338292554`
   (Requires caller to be a Global Admin or Privileged Role Admin. If this fails, do it via Azure portal: Entra ID → App registrations → TeamsMediaBotPOC → API permissions → Grant admin consent.)

## Phase 4 — Windows VM + C# bot in westus (~30 min + manual)

1. `az vm create -g rg-teams-bot-westus -n vm-tbot-westus --image Win2022Datacenter --size Standard_D4s_v3 --admin-username azureuser --admin-password 'SecureTeamsBot2026!' --public-ip-sku Standard --public-ip-address-allocation static --nsg-rule NONE`
2. `az vm open-port` for 443, 8445, 3389 on the NSG (priorities 1000–1002).
3. Capture new VM public IP.
4. **Manual (user)**: Update GoDaddy DNS:
   - `teamsbot` A → new VM IP
   - `media` A → new VM IP
5. **Manual (user)**: SSL cert for `teamsbot.qmachina.com` and `media.qmachina.com` on the VM. Options:
   - (a) Provide a PFX (existing wildcard cert if available) — I'll import via PowerShell.
   - (b) Use win-acme / Certify the Web for Let's Encrypt — needs port 80 briefly during DNS-01 or HTTP-01 challenge.
6. `az vm run-command invoke --command-id RunPowerShellScript` to bootstrap:
   - Install .NET 8 SDK (winget or direct download)
   - Install git (winget)
   - Install nssm
   - `git clone https://github.com/logan-robbins/teams-bot-poc.git C:\teams-bot-poc`
   - Generate `C:\teams-bot-poc\src\Config\appsettings.production.json` with real values (AppId, AppSecret, TenantId, NotificationUrl, ServiceFqdn, CertThumbprint, InstancePublicIPAddress, TranscriptSink.PythonEndpoint=https://agent.qmachina.com/transcript, STT creds)
   - `dotnet publish -c Release`
   - `nssm install TeamsMediaBot <path-to-TeamsMediaBot.exe>` with `--config C:\teams-bot-poc\src\Config\appsettings.production.json`
   - `Start-Service TeamsMediaBot`
7. **Patch `scripts/deploy-production.ps1`** to add the missing clone step before the build (current script assumes code already at `C:\teams-bot-poc`).

## Phase 5 — Teams app install (5 min, manual)

1. `cd manifest && zip -r ../teams-bot-poc.zip manifest.json color.png outline.png`
2. **Manual (user)**: Sideload zip in Teams admin center (Apps → Manage apps → Upload new app) OR upload to a specific team as a custom app.
3. Add bot to a test meeting (@mention the bot in meeting chat, or add via participants panel if your tenant allows it).

## Phase 6 — end-to-end test

1. Start Teams meeting → invite bot.
2. Tail Serilog on VM: `Get-Content C:\teams-bot-poc\src\bin\Release\net8.0\logs\teamsbot-*.log -Wait -Tail 50`.
3. Expect: `"Incoming call received"` → `"Answered incoming call"` → `"Call added to collection"` → audio frames flowing → transcripts POST to `/transcript`.
4. Browse `https://interview.qmachina.com` — live InterviewAnalysis output.

---

## Ownership split

### I execute
- Phase 0, 1 (Azure resource creation), 2 (C# code edit), 3 (`az bot create`), 4 (VM create + PowerShell bootstrap via `az vm run-command`), 5 (build zip), 6 (log tailing + validation).

### User executes (will prompt with exact records/steps)
- GoDaddy DNS updates ×3 rounds (agent/interview after Phase 1, teamsbot/media after Phase 4).
- Admin consent if `az ad app permission admin-consent` fails (portal click).
- SSL cert provisioning for `teamsbot.qmachina.com` + `media.qmachina.com` (PFX or Let's Encrypt approval).
- Teams app sideload.
- Join a test Teams meeting and invite the bot.

## Risks / unknowns

- **gpt-5-mini in westus**: may not have capacity for GlobalStandard. Fallback: place AOAI in `westus3` or `eastus`.
- **`az bot create` permissions**: requires privileged role on tenant. If it fails, do it via portal.
- **`deploy-production.ps1` gap**: no repo clone step. Will patch before running.
- **SSL cert** for `media.qmachina.com` is a hard requirement — Graph Communications Media SDK refuses to start without a valid cert matching `ServiceFqdn`.
- **Notification-URL cert**: Graph also requires the notification URL (`teamsbot.qmachina.com`) to be HTTPS with a publicly-trusted cert.

## Active-time estimate

~90 min of my work + your DNS/consent/sideload interleaved.

---

## Post-deploy checklist

- [ ] `curl https://agent.qmachina.com/health` → 200
- [ ] `curl https://teamsbot.qmachina.com/api/calling/health` → 200
- [ ] `https://interview.qmachina.com` loads Streamlit UI
- [ ] Azure Bot channel "Microsoft Teams" shows Running
- [ ] App Registration Graph permissions show "Granted for <tenant>"
- [ ] Bot sideloaded in Teams; shows calling icon in app catalog
- [ ] Invited bot to test meeting; Serilog shows `"Answered incoming call"`
- [ ] Transcript events hit `/transcript` with non-empty text
- [ ] Streamlit shows live running-assessment updates
