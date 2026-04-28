# NOTES — Alfred Sandbox Deployment

Date: 2026-04-27. Branch: `feat/alfred-chat-modality`. For AI session continuity.

## Tenant + subscription state

- Target tenant: Disney sandbox `38387f0b-9a6f-46e2-8373-67422f8c2cb0`. Identity `Logan.Robbins@plutosdoghouse.onmicrosoft.com`.
- **No Azure subscription scoped to this user in 38387f.** Blocks every `Microsoft.*` resource (VM, OpenAI, Speech, Container Apps, Bot resource).
- Visible elsewhere: qMachina sub `70464868-52ea-435d-93a6-8002e83f0b89` in tenant `2843abed-...` (identity `logan@qmachina.com`). Used by the previous deployment.
- Disney process: Sandbox first (light scope approval by an engineer) → ESIS review → Production. Lock the scope set in Sandbox before ESIS.

## Created this session

Entra app reg in 38387f (`az ad app create`):

- Display name: `Alfred-Sandbox`
- App ID: `e68b49d1-0aae-4761-a595-4df482d8d4fe`
- Object ID: `8ce15110-228d-44f8-9c82-870ca10ca33a`
- SP Object ID: `85ed689d-cb7c-4b44-9ef4-8b5b9b70794a`
- Audience: `AzureADMyOrg` (single-tenant)
- Secret: `alfred-sandbox-bootstrap-secret`, ~1y. Value held by user only.
- Graph perms requested, **not admin-consented**: `Calls.AccessMedia.All`, `Calls.JoinGroupCall.All`.

Manifest committed: `manifest/manifest.json` `id` and `bots[0].botId` rewritten to the new App ID.

## Required Graph scope set (for ESIS)

App-level (admin consent):
1. `Calls.AccessMedia.All`
2. `Calls.JoinGroupCall.All`

Resource-Specific Consent (per-chat at install, declared in manifest):
3. `ChatMessage.Read.Chat`
4. `ChatMessageReadReceipt.Read.Chat`
5. `OnlineMeetingParticipant.Read.Chat`
6. `OnlineMeeting.ReadBasic.Chat`
7. `Calls.AccessMedia.Chat`
8. `Calls.JoinGroupCalls.Chat`
9. `TeamsActivity.Send.Chat`

Future-flag only if `policy_auto_invite` is enabled: `OnlineMeetings.Read.All`. Default mode does not need it.

## IT asks (Disney sandbox, in 38387f)

1. Admin-consent the two app-level Graph perms on App ID `e68b49d1-0aae-4761-a595-4df482d8d4fe`.
2. After upload of `manifest/teams-bot-poc.zip`, set `Alfred — Meeting Assistant` (package `com.qmachina.alfred`) to **Allowed** in Teams Admin Center.
3. Add the app to a Teams app permission policy → assign the policy to a security group of pilot users.
4. Create a resource account for one-click "Add people → Alfred":
   ```
   New-CsOnlineApplicationInstance `
     -UserPrincipalName "alfred@plutosdoghouse.onmicrosoft.com" `
     -DisplayName "Alfred" `
     -ApplicationId "e68b49d1-0aae-4761-a595-4df482d8d4fe"
   Sync-CsOnlineApplicationInstance -ObjectId "<from above>"
   ```
   Assign the free "Microsoft Teams Phone Resource Account" license to that UPN.

After (1)–(4), the pilot users' "+ Add people" picker resolves "Alfred" and one click brings the bot into a meeting. **The bot already answers incoming calls** — `src/Services/TeamsCallingBotService.cs:187` (`OnIncoming` subscription) and `:230` (`AnswerAsync`). No code change needed for this UX.

## Pending decisions (block standing up Azure compute)

- **Subscription path**:
  - A) Disney for identity (38387f), qMachina sub for compute. Recommended for sandbox/POC.
  - B) Wait on a Disney-provisioned sub in 38387f.
  - C) Pure qMachina. Loses the Disney use case.
- **Bot public hostname**: keep `teamsbot.qmachina.com` (DNS in qMachina) or use the VM's free `*.cloudapp.azure.com` and skip custom domain.
- **React UI deploy**: new `web/Dockerfile` + Container App (best), static-serve `web/dist` from FastAPI, or local-only against the cloud sink.
- **VM admin password**: `scripts/deploy-azure-vm.sh` hardcodes `SecureTeamsBot2026!`. Override before run.

## Code gaps to fix before running deploy scripts

`scripts/deploy-azure-agent.sh` will boot-loop the Python container as written:

- `python/Dockerfile` CMD invokes `uvicorn transcript_sink:app` directly.
- `transcript_sink.py::load_runtime_config()` (line 99 onward) raises `RuntimeError` unless these env vars are present on the container:
  - `PRODUCT_SPEC_PATH=legionmeet_platform/specs/alfred.yaml`
  - `VARIANT_ID=alfred`
  - `INSTANCE_ID=alfred`
  - (`SINK_HOST`, `SINK_PORT` default OK)
- After VM exists, also set `BOT_SEND_CHAT_URL=https://<bot-fqdn>/api/send-chat` so the `send_to_meeting_chat` tool posts instead of dry-running.

Add these to the `--env-vars` list in `deploy-azure-agent.sh` before invoking it.

`web/` has no Dockerfile. Either add one (multi-stage `npm run build` → nginx serve `dist/`) or run the React UI locally pointed at the cloud sink (Vite proxies `/sink/*` to `SINK_URL`).

## Public-endpoint constraint

Two paths *must* be publicly reachable HTTPS, period:

- Teams calling webhook (`/api/calling`)
- Graph change-notification webhook

Microsoft cloud → your bot. No Private Link / VPN / ExpressRoute path delivers these into a customer VNet. Lock down with NSG service-tag allowlists (`AzureBotService`, `AzureCloud.<region>`) + AAD JWT validation in code. Everything else (Python sink, React UI, OpenAI, Speech) can be VNet-internal via Container Apps internal ingress + private endpoints.

## Next-step checklist

1. User decides subscription path (A/B/C) and DNS choice.
2. User sends Disney IT the four asks above.
3. User runs `cd manifest && zip -r teams-bot-poc.zip manifest.json color.png outline.png` and uploads to sandbox Teams Admin Center.
4. When subscription + admin consent are in place:
   - Verify `gpt-5-mini` availability in chosen region (`az cognitiveservices model list`).
   - Patch `scripts/deploy-azure-agent.sh` env vars per the gap list above.
   - Run `setup-azure-mac.sh` (Bot + Speech), `deploy-azure-vm.sh` (VM), `deploy-azure-agent.sh` (OpenAI + Container Apps).
   - Issue TLS for the chosen bot hostname (win-acme on the VM).
   - Update bot `src/Config/appsettings.json`: App ID `e68b49d1-...`, the secret, tenant `38387f0b-...`, cert thumbprint.
   - Test via "+ Add people → Alfred" with a pilot user.

## Key files

- `ALFRED.md` — system architecture (canonical)
- `docs/TEAMS-AUTO-INVITE-SETUP.md` — compliance recording auto-invite flow
- `manifest/manifest.json` — already updated to App ID `e68b49d1-...`
- `src/Config/appsettings.example.json` — bot config schema (still has placeholder values)
- `scripts/setup-azure-mac.sh`, `scripts/deploy-azure-vm.sh`, `scripts/deploy-azure-agent.sh` — deploy scripts (ALFRED gaps noted above)
- `scripts/setup-policy-auto-invite.ps1` — PowerShell for compliance recording mode
