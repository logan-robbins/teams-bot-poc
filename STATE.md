# STATE — Alfred eastus deploy (2026-04-28, COMPLETE)

## Goal

Stand Alfred up end-to-end in qMachina sub `70464868-...`, eastus, RG `rg-alfred-poc`, on the `feat/alfred-chat-modality` branch. Replace the previous westus Talestral deployment.

## Inventory — what is up and verified

| Resource | Name | State |
|---|---|---|
| Resource group | `rg-alfred-poc` (eastus) | Created |
| Entra app reg | `Alfred` (App ID `ff4b0902-5ae8-450b-bf45-7e2338292554`) | Renamed; admin-consented from prior deploy; client secret stored in `/tmp/app-secret.json` on dev machine (never commit) |
| Azure Bot | `alfred-bot-qmachina` | Created, single-tenant, Teams channel + calling enabled. `messaging=https://teamsbot.qmachina.com/api/messages`, `calling=https://teamsbot.qmachina.com/api/calling` |
| Azure OpenAI | `aoai-alfred` + `gpt-5-mini` deployment | S0, capacity 10 GlobalStandard |
| Azure Speech | `speech-alfred` | S0 |
| ACR | `acralfredpoc70464868` | Holds `ca-alfred-api`, `ca-alfred-web` images |
| Log Analytics | `workspace-rgalfredpocjAKn` | Auto-created with CAE |
| Container Apps env | `cae-alfred` | Default domain `orangecoast-aa65f885.eastus.azurecontainerapps.io` |
| Container App: FastAPI sink | `ca-alfred-api` | **Healthy.** `/health` returns `variant_id=alfred, product_id=alfred` |
| Container App: React UI | `ca-alfred-web` | **Healthy.** Loads, nginx `/sink/*` proxy reaches sink (SNI fix shipped) |
| VM | `vm-alfred` | Running, Standard_D4s_v3, public IP `172.190.7.169`, FQDN `vm-alfred-eastus.eastus.cloudapp.azure.com` |
| NSG `vm-alfredNSG` | rules 1000–1003 | Open 80/443/8445/3389 |
| VM contents | `C:\teams-bot-poc` | Repo cloned at `feat/alfred-chat-modality`; `dotnet publish` succeeded; `TeamsMediaBot.exe` + `NativeMedia.dll` present at `src\bin\Release\net8.0\publish` |
| VM prereqs | git, dotnet 8 SDK, nssm, Chocolatey, **Server-Media-Foundation, VCRedist 140, OpenSSH Server** | All present |
| TLS cert | `qmachina-teamsbot-media` (CN=teamsbot.qmachina.com, SAN=media.qmachina.com), thumbprint `BF4F6A01402DCAF38B71A8E6193E3711CCC3D132`, issued by Let's Encrypt R12, valid Apr 28 → Jul 27 2026 | Installed in `Cert:\LocalMachine\My`, win-acme renewal task scheduled |
| Windows service | `TeamsMediaBot` (nssm) | **Running**, StartType=Automatic, listening on `https://[::]:443`, MediaEndpoint `media.qmachina.com:8445` |
| Public health | `https://teamsbot.qmachina.com/api/calling/health` → `200 {"status":"Healthy","service":"Alfred"}` | Verified 2026-04-28 20:27 UTC |
| NSG | rules 1000–1005: 80/443/8445/3389 + recovery rules 22 (SSH) and 5986 (WinRM HTTPS) | Open |
| DNS | `teamsbot/media → 172.190.7.169`, `agent/alfred → CAE FQDNs` | All resolving correctly |
| Westus teardown | `rg-teams-bot-westus` | Deleted |

## Recovery 2026-04-28

The deploy went through three classes of failure before reaching healthy:

1. **Wedged action Run Command extension** — recovered via RDP cleanup of orphaned `C:\Packages\Plugins\Microsoft.CPlat.Core.RunCommandWindows*` plus stale `aggregatestatus.json` and `RdAgent`/`WindowsAzureGuestAgent` restart. Root cause and prevention captured in memory: `feedback_run_command_extension_orphans.md`, `feedback_use_managed_run_command.md`.
2. **ARM instance-view cache lag** showed `vmAgent: null` while data plane was fully working — captured in memory `feedback_arm_instance_view_lag.md` and codified in `deploy-azure-vm.sh::probe_agent_via_run_command`.
3. **Bot crashloop on startup** with `DllNotFoundException: NativeMedia` — root cause was missing `Server-Media-Foundation` Windows feature and VC++ 2015–2022 Redistributable. Captured in memory `feedback_graph_communications_media_prereqs.md` and codified in `bootstrap-production-vm.ps1::Install-MediaPlatformPrereqs`.

## Bootstrap re-run command

```bash
./scripts/deploy-azure-vm.sh
```

Reads secret files from `/tmp/app-secret.json`, `/tmp/vm-admin-pass.txt`, `/tmp/speech-key.txt`. Idempotent — safe to re-run for config refresh, cert renewal trigger, or service reinstall.

## Secret files (kept on dev machine)

- `/tmp/app-secret.json` — `{appId, password}` for Entra app `Alfred`. Required by deploy script.
- `/tmp/vm-admin-pass.txt` — VM `azureuser` password. Required by deploy script (service runs under this account).
- `/tmp/speech-key.txt` — Azure Speech key. Required by deploy script.
- `/tmp/aoai-key.txt` — Azure OpenAI key. Used by Container App env vars (already configured at CAE level).
