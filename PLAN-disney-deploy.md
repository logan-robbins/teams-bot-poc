# Alfred — Disney sandbox backend deploy plan

Living document. Updated after each successful step. If a session is interrupted, resume from the first non-`[done]` line.

## Target

| Field | Value |
|---|---|
| Azure subscription | `e02c0038-82c8-4655-9647-38083f301099` (`WDI R&D`) |
| Azure tenant | `56b731a8-a2ac-4c32-bf6b-616810e913c6` (`disney.com`) |
| Identity | `Logan.Robbins@disney.com` |
| Disney M365 tenant (where the Teams app + Entra app live) | `38387f0b-9a6f-46e2-8373-67422f8c2cb0` (`plutosdoghouse.com`) |
| Entra app for the bot | `Alfred` `207a38a4-67c5-4ef9-ada8-ea7998734d59` (multi-tenant, in M365 tenant) |
| Region | `eastus` |
| Resource group | `rg-alfred-disney` |
| Naming convention | `*-alfred-disney` (no `qmachina` anywhere) |
| Bot FQDN | `alfred-disney-bot.eastus.cloudapp.azure.com` (Azure-managed; no external DNS) |

## Branding rule

**Nothing in deployed config, env vars, or resource names contains `qmachina`.** If you see one slip through, fail the step.

---

## Phase 1 — Foundation resources

- [x] 1.1 Create resource group `rg-alfred-disney` (eastus) — done
- [x] 1.2 Create Azure OpenAI account `aoai-alfred-disney` (S0, custom-domain enabled) — done; endpoint `https://aoai-alfred-disney.openai.azure.com/`
- [x] 1.3 Deploy `gpt-5-mini` (model version `2025-08-07`, `GlobalStandard`, capacity 10) — done
- [x] 1.4 Create Azure Speech `speech-alfred-disney` (S0, eastus) — done
- [x] 1.5 Create Azure Container Registry `acralfreddisneye02c0038` (Standard SKU, admin user enabled) — done; `acralfreddisneye02c0038.azurecr.io`
- [x] 1.6 Create Container Apps environment `cae-alfred-disney` (eastus) — done; default domain `gentlewater-5aa74a73.eastus.azurecontainerapps.io` (also registered providers Microsoft.App, Microsoft.BotService, Microsoft.OperationalInsights, Microsoft.Insights, Microsoft.ContainerService)

## Phase 2 — Container Apps (sink + UI)

- [x] 2.1 Build sink image from `python/` via `acr build`, tag `:disney-sandbox-1` — done; digest `sha256:7cf55f94...`
- [x] 2.2 Create Container App `ca-alfred-api` (target port 8765, external ingress, scale 0–3) — done; FQDN `ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io`; `/health` returns `{"status":"healthy",...}`. Note: had to wire ACR admin creds via `az containerapp registry set` because the SP creating the app lacked RBAC role-assignment perms; quickstart placeholder image got installed first then replaced.
- [x] 2.3 Build UI image from `web/` via `acr build`, tag `:disney-sandbox-1` — done. Hit Docker Hub rate limits on both `nginx:alpine` and `node:20-alpine`. Workaround: `az acr import` cached nginx into ACR, created `web/Dockerfile.disney-acrcache` that pulls both base images from `acralfreddisneye02c0038.azurecr.io` instead of Docker Hub, build went through.
- [x] 2.4 Create Container App `ca-alfred-web` (target port 80, external ingress, scale 0–2) — done; FQDN `ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io`
- [x] 2.5 Confirm sink `/health` and UI return 200 on default CAE FQDNs — done. Sink: `{"status":"healthy",...}`. UI: HTTP 200.

## Phase 3 — Bot Entra client secret + Azure Bot Service

- [x] 3.1 Switch CLI context: `az login --tenant 38387f0b-... --allow-no-subscriptions` — done
- [x] 3.2 Create client secret on Entra app `207a38a4-...`, save to `/tmp/alfred-disney-app-secret.json` — done; secret display name `alfred-disney-vm-bot-2026-04`, expires 2027-04. **Rotate after sandbox deploy stabilizes** (value was emitted to this transcript).
- [x] 3.3 Switch back: `az account set --subscription e02c0038-...` — done
- [x] 3.4 Create Azure Bot Service `bot-alfred-disney` — done. Used **SingleTenant** (multi-tenant deprecated July 2025); MicrosoftAppId=`207a38a4-...`, MicrosoftAppTenantId=`38387f0b-...` (Disney M365 where the Entra app lives), endpoint `https://alfred-disney-bot.eastus.cloudapp.azure.com/api/messages`. Conflict resolution: had to delete the prior Developer Portal AND legacy `dev.botframework.com` registrations to free the appId.
- [x] 3.5 Add Microsoft Teams channel — done
- [x] 3.6 Enable Calling on the Teams channel with calling webhook `https://alfred-disney-bot.eastus.cloudapp.azure.com/api/calling` — done

## Phase 4 — Windows VM bot

- [x] 4.1 Create static public IP `pip-alfred-disney` with DNS label `alfred-disney-bot` (FQDN: `alfred-disney-bot.eastus.cloudapp.azure.com`) — done; IP `104.41.132.9`
- [x] 4.2 Create NSG `nsg-alfred-disney` with inbound rules: 22, 80, 443, 3389, 5986, 8445 — done
- [x] 4.3 Create Windows Server 2022 VM `vm-alfred-disney` — done. **Resized D2s_v3 → D4s_v3** mid-deploy because the Graph Communications media SDK requires ≥2 *physical* cores (D2s_v3 = 1 physical × 2 SMT threads → SDK rejected). D4s_v3 = 2 physical × 2 SMT.
- [x] 4.4 Bootstrap install — done. `bootstrap-production-vm.ps1` updated: replaced Chocolatey-based dotnet install (Choco CDN returned 504) with Microsoft's official `dotnet-install.ps1` from `https://dot.net/v1/dotnet-install.ps1`; new helper `Install-DotnetSdkIfMissing` checks `dotnet --list-sdks` instead of just `Get-Command dotnet` (Windows ships a non-SDK `dotnet.exe` that fooled the prior check).
- [x] 4.5 Repo sync + `dotnet publish` — done. `deploy-azure-vm.sh` got a new `SKIP_REPO_SYNC` env var (default 1 preserves qMachina behavior; we ran with 0 for the first-time clone).
- [x] 4.6 Open Windows Firewall rules for 80, 443, 8445 — done
- [x] 4.7 Install win-acme — done
- [x] 4.8 Issue Let's Encrypt cert for `alfred-disney-bot.eastus.cloudapp.azure.com` (HTTP-01) — done. Thumbprint `C69FA206DD6F22D15A3DBE05D8DC4556BC321C08`, friendlyName `alfred-disney-cert`, Task Scheduler renewal registered. `vm-request-letsencrypt-cert.ps1` parameterized with `-Hostnames`, `-EmailAddress`, `-FriendlyName`.
- [x] 4.9 Generate `appsettings.production.json` with Disney values (no qmachina) — done; verified at runtime via bot startup log
- [x] 4.10 Install + start `TeamsMediaBot` Windows service via nssm — done. `vm-finalize-bootstrap.ps1` parameterized with `-CertSubjectHosts`, `-CertFriendlyNamePattern`. Service Status: Running, listening on 443 + 8445, log shows "Teams Calling Bot Service initialized successfully".

## Phase 5 — Wire endpoints + validate

- [x] 5.1 Update `bot-alfred-disney` messaging endpoint — done at create time (Phase 3.4); `https://alfred-disney-bot.eastus.cloudapp.azure.com/api/messages`
- [x] 5.2 Update Teams channel calling webhook — done at create time (Phase 3.6); `https://alfred-disney-bot.eastus.cloudapp.azure.com/api/calling`
- [x] 5.3 Update `ca-alfred-api` env: add `BOT_SEND_CHAT_URL=https://alfred-disney-bot.eastus.cloudapp.azure.com/api/send-chat` — done
- [x] 5.4 Restart `TeamsMediaBot` service — done (after VM resize); clean start, no errors
- [x] 5.5 Public health probes — done. Bot: `{"status":"Healthy","activeCalls":0}`. Sink: `{"status":"healthy","agent_available":true,"variant_id":"alfred"}`. UI: HTTP 200.
- [x] 5.6 Update Disney manifest's `validDomains` to the deployed FQDNs — done; `manifest/disney/alfred-sandbox.zip` v1.0.6 now lists the VM FQDN and both CAE FQDNs
- [x] 5.7 Document final endpoint table at the bottom of this file — done

## Out of scope (parking lot)

- DNS aliases on `disney.com` or `plutosdoghouse.com` — using Azure FQDNs only
- RTM media allowlist for app id `207a38a4-...` — request submitted in parallel via aka.ms/teams-rtm-onboarding; calling won't fully work until approved
- Custom domains on the Container Apps — sandbox uses default `*.azurecontainerapps.io` FQDNs
- Auto-renewal cron jobs other than Let's Encrypt's built-in
- Multi-region / HA — sandbox is single-region

---

## Final endpoint table

| Surface | URL | Verified |
|---|---|---|
| Bot health | `https://alfred-disney-bot.eastus.cloudapp.azure.com/api/calling/health` | ✓ `{"status":"Healthy","service":"Alfred","activeCalls":0}` |
| Bot messaging (Bot Framework target) | `https://alfred-disney-bot.eastus.cloudapp.azure.com/api/messages` | wired in Azure Bot Service `bot-alfred-disney` |
| Bot calling webhook (Teams channel) | `https://alfred-disney-bot.eastus.cloudapp.azure.com/api/calling` | wired in Teams channel config |
| Bot send-chat (used by sink agent) | `https://alfred-disney-bot.eastus.cloudapp.azure.com/api/send-chat` | env on `ca-alfred-api` |
| Sink (Container App) | `https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io` | ✓ `/health` returns healthy |
| UI (Container App) | `https://ca-alfred-web.gentlewater-5aa74a73.eastus.azurecontainerapps.io` | ✓ HTTP 200 |
| Azure Bot Service | `bot-alfred-disney` (SingleTenant, app tenant `38387f0b-...`) | ✓ |
| Entra app (bot identity) | `Alfred` `207a38a4-67c5-4ef9-ada8-ea7998734d59` (in M365 tenant `38387f0b-...`, multi-tenant `AzureADMultipleOrgs`) | ✓ secret stored in `/tmp/alfred-disney-app-secret.json` (rotate after stable) |
| TLS cert | `CN=alfred-disney-bot.eastus.cloudapp.azure.com`, thumb `C69FA206DD6F22D15A3DBE05D8DC4556BC321C08`, Let's Encrypt, expires ~90 days, auto-renew via Task Scheduler | ✓ |
| Teams app manifest | `manifest/disney/alfred-sandbox.zip` v1.0.6 — branding "The Walt Disney Company", botId `207a38a4-...`, RSC-only perms, validDomains list deployed FQDNs | re-upload in Developer Portal |

## Open items: **Disney M365 admin (Global Administrator) action required**

End-user installation of Alfred Sandbox is being blocked by Disney's tenant-level "user can't consent to unverified publishers" policy. This is NOT a manifest issue — verified on 2026-04-29 that all 5 RSC perms in `manifest/disney/alfred-sandbox.zip` v1.0.6 are already `type: Application` (the correct type for our app-only bot). Logan has no admin role in `38387f0b-...`, so the next step has to be opened with Disney IT.

**Pick ONE of the two paths:**

### Path A — Teams Admin Center, click-through (recommended, takes ~2 min)

1. Sign in to `https://admin.teams.microsoft.com/policies/manage-apps` as a Global Administrator (or Teams Administrator).
2. Search for **Alfred Sandbox** (Teams app id `207a38a4-67c5-4ef9-ada8-ea7998734d59`).
3. Open the app → **Permissions** tab.
4. Under **Resource-specific consent (RSC) permissions**, you will see the 5 chat-scope Application permissions:
   - `ChatMessage.Read.Chat`
   - `ChatMessageReadReceipt.Read.Chat`
   - `OnlineMeetingParticipant.Read.Chat`
   - `Calls.AccessMedia.Chat`
   - `Calls.JoinGroupCalls.Chat`
5. Click **Grant admin consent** (button typically on the Permissions tab). Accept the permissions list.
6. After consent, end users (chat owners / meeting organizers) can install Alfred Sandbox without their own consent prompt.

### Path B — Microsoft Graph PowerShell (preview)

For admins who prefer code over click-through. **Requires Global Administrator** and the `Microsoft.Graph.Beta.Teams` module.

```powershell
Install-Module Microsoft.Graph.Beta.Teams -Scope CurrentUser -Force
Connect-MgGraph -Scopes 'TeamworkAppSettings.ReadWrite.All','Policy.ReadWrite.PermissionGrant','AppCatalog.Read.All','Application.ReadWrite.All' -TenantId 38387f0b-9a6f-46e2-8373-67422f8c2cb0

New-MgBetaTeamAppPreapproval `
  -TeamsAppId 207a38a4-67c5-4ef9-ada8-ea7998734d59 `
  -ResourceSpecificApplicationPermissionsAllowedForChats @(
    'ChatMessage.Read.Chat',
    'ChatMessageReadReceipt.Read.Chat',
    'OnlineMeetingParticipant.Read.Chat',
    'Calls.AccessMedia.Chat',
    'Calls.JoinGroupCalls.Chat'
  )
```

(`New-MgBetaTeamAppPreapproval` is in **public preview** per Microsoft docs. If the tenant doesn't support preview commands, fall back to Path A.)

### Why neither Logan nor I can do this from CLI

- `az rest GET /beta/teamwork/teamsAppSettings` returns **403 Forbidden** — the az CLI's default Graph token lacks `TeamworkAppSettings.ReadWrite.All`.
- `me/transitiveMemberOf/microsoft.graph.directoryRole` returns **`[]`** — Logan holds no directory role in Disney M365 tenant `38387f0b-...`. Only Global Admin or Teams Administrator can grant the consent or run the preapproval cmdlet.

### Why the manifest is fine as-is (do NOT change perm types)

- All 5 RSC perms are already declared with `"type": "Application"` — the correct type for an app-only bot like Alfred (the bot runs as a service principal, no signed-in user).
- Switching to `Delegated` would require a signed-in user context that the bot doesn't have, and would silently break the SDK's auth path even if Teams accepted the upload.
- The manifest's `defaultBlockUntilAdminAction` flag is **explicitly NOT supported for custom org apps** per Microsoft docs (`learn.microsoft.com/microsoftteams/platform/concepts/deploy-and-publish/add-default-install-scope#block-apps-by-default-for-users-until-an-admin-approves`), so adding it is a no-op for Alfred Sandbox.

---

## Other open items (out of automated scope)

- Re-upload `manifest/disney/alfred-sandbox.zip` v1.0.6 in Developer Portal so install reflects latest validDomains.
- Submit RTM media allowlist request for app id `207a38a4-...` at `https://aka.ms/teams-rtm-onboarding`. Until approved, joining a meeting and capturing audio may fail — install + RSC consent dialog will still work.
- Rotate the bot client secret `alfred-disney-vm-bot-2026-04` after the sandbox stabilizes (the value was emitted to a chat transcript).
- Delete the orphaned `Alfred-Sandbox` Entra app `e68b49d1-0aae-4761-a595-4df482d8d4fe` in M365 tenant — no longer referenced by any infra.
- Optional: delete `manifest/outline.png.bak` and `web/Dockerfile.disney-acrcache` if not needed for re-deploy.

## Code changes made to support this deploy

- `scripts/bootstrap-production-vm.ps1`: new `Install-DotnetSdkIfMissing` (uses `dotnet-install.ps1`, replaces broken Choco-only path)
- `scripts/vm-request-letsencrypt-cert.ps1`: parameterized `-Hostnames`, `-EmailAddress`, `-FriendlyName`
- `scripts/vm-finalize-bootstrap.ps1`: parameterized `-CertSubjectHosts`, `-CertFriendlyNamePattern`
- `scripts/deploy-azure-vm.sh`: new `SKIP_REPO_SYNC` env var (defaults 1 = preserve qMachina), passes hostname/email/friendly-name to cert and finalize phases

All changes are backwards-compatible with the qMachina deploy (defaults preserve original behavior).
