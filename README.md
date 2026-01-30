# Teams Media Bot POC

Teams calling bot that joins meetings, receives real-time audio, transcribes with Azure Speech, and streams transcripts to a Python service.

**Status:** Deployed and operational on Azure Windows VM  
**Domain:** `teamsbot.qmachina.com` / `media.qmachina.com`  
**VM:** `52.188.117.153` (Windows Server 2022, D4s_v3)

---

## Architecture (Current Working System)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Microsoft Teams Meeting                          │
│                     (Audio/Video from participants)                      │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 │ Graph API Call Events
                                 │ (IncomingCall, Updated, etc.)
                                 ↓
                    ┌────────────────────────────┐
                    │   Azure Bot Service        │
                    │   (teams-media-bot-poc)    │
                    └────────────┬───────────────┘
                                 │
                                 │ Webhook POST (public)
                                 │ https://teamsbot.qmachina.com/api/calling  (TLS :443)
                                 ↓
┌────────────────────────────────────────────────────────────────────────┐
│                  Azure Windows VM (52.188.117.153)                      │
│                         Windows Server 2022 D4s_v3                      │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────┐     │
│  │ Ingress / TLS termination                                     │     │
│  │  • Public endpoint: :443                                      │     │
│  │  • Forwards to bot listener: https://127.0.0.1:9443           │     │
│  └───────────────────────────────────────────────────────────────┘     │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────┐     │
│  │ TeamsMediaBot Windows Service (C# .NET 8, ASP.NET Core)        │     │
│  │ Running as .\azureuser (required for LocalMachine\\My cert)     │     │
│  │                                                                │     │
│  │  ┌─────────────────────────────────────────────────────────┐  │     │
│  │  │ Kestrel HTTPS backend (internal)                         │  │     │
│  │  │  • https://0.0.0.0:9443                                  │  │     │
│  │  │  • Certificate loaded by thumbprint                      │  │     │
│  │  └──────────────────┬──────────────────────────────────────┘  │     │
│  │                     │                                          │     │
│  │                     ↓                                          │     │
│  │  ┌─────────────────────────────────────────────────────────┐  │     │
│  │  │ CallingController.cs                                     │  │     │
│  │  │  • POST /api/calling (Graph notifications)               │  │     │
│  │  │  • POST /api/calling/join (manual trigger)               │  │     │
│  │  │  • GET  /api/calling/health                              │  │     │
│  │  └──────────────────┬──────────────────────────────────────┘  │     │
│  │                     │                                          │     │
│  │                     ↓                                          │     │
│  │  ┌─────────────────────────────────────────────────────────┐  │     │
│  │  │ TeamsCallingBotService.cs / Media SDK                    │  │     │
│  │  │  • Joins meeting via Graph Communications SDK            │  │     │
│  │  │  • Receives real-time audio on :8445                     │  │     │
│  │  └──────────────────┬──────────────────────────────────────┘  │     │
│  │                     │ Raw audio frames (PCM 16kHz)            │     │
│  │                     ↓                                          │     │
│  │  ┌─────────────────────────────────────────────────────────┐  │     │
│  │  │ CallHandler.cs                                           │  │     │
│  │  │  • Buffers audio and pushes to transcriber               │  │     │
│  │  └──────────────────┬──────────────────────────────────────┘  │     │
│  │                     │                                          │     │
│  │                     ↓                                          │     │
│  │  ┌─────────────────────────────────────────────────────────┐  │     │
│  │  │ AzureSpeechRealtimeTranscriber.cs                        │  │     │
│  │  │  • Speech SDK continuous recognition                     │  │     │
│  │  │  • Emits partial + final transcripts                     │  │     │
│  │  └──────────────────┬──────────────────────────────────────┘  │     │
│  │                     │                                          │     │
│  │                     ↓                                          │     │
│  │  ┌─────────────────────────────────────────────────────────┐  │     │
│  │  │ PythonTranscriptPublisher.cs                             │  │     │
│  │  │  • POST → http://127.0.0.1:8765/transcript               │  │     │
│  │  │  • Also saves: Desktop\\meeting_transcript.txt           │  │     │
│  │  └─────────────────────────────────────────────────────────┘  │     │
│  └───────────────────────────────────────────────────────────────┘     │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────┐     │
│  │ Python Transcript Sink (optional, same VM or elsewhere)        │     │
│  │  • `python/transcript_sink.py` (FastAPI)                       │     │
│  │  • POST /transcript :8765                                      │     │
│  └───────────────────────────────────────────────────────────────┘     │
└────────────────────────────────────────────────────────────────────────┘
                                                     wss:// (Speech SDK)
                                             ┌────────────────────────────┐
                                             │  Azure Speech Service       │
                                             │  (speech-teams-bot-poc)     │
                                             │  Region: eastus             │
                                             └────────────────────────────┘


Current Trigger Method:
  curl -X POST https://teamsbot.qmachina.com/api/calling/join \
    -H "Content-Type: application/json" \
    -d '{"joinUrl":"TEAMS_MEETING_URL","displayName":"Transcription Bot"}'
```

### Ingress / :443 forwarding (what the code expects)

The repo’s current configuration binds the bot’s **Kestrel backend to `https://0.0.0.0:9443`** (see `src/Config/appsettings.json`). Because the Azure NSG opens **:443** publicly (and the webhook URL is `https://teamsbot.qmachina.com/...`), production requires a **reverse proxy on the VM** to accept public TLS on :443 and forward traffic to the local bot backend on :9443.

Implementation-wise this can be IIS, nginx, Caddy, or any other layer that performs **:443 → :9443** forwarding. The important point is: **the bot process itself is not listening on :443 in the current config**.

### STT provider/model selection (no fan-out)

- **Source of truth**: the C# code creates **one transcriber per call** and pushes Teams PCM frames into it.
- **Choosing provider**: STT selection is now config-driven via `Stt.Provider` (currently supports `AzureSpeech`; other providers can be added behind the same interface).
- **Choosing model (Azure Speech)**: you can select a specific Custom Speech model by setting an **EndpointId** in `Stt.AzureSpeech.EndpointId` (optional).
- **No fan-out**: the bot does **not** stream the same audio to multiple STT providers/models.

Example (Azure Speech with optional Custom Speech model):

```json
{
  "Stt": {
    "Provider": "AzureSpeech",
    "AzureSpeech": {
      "Key": "YOUR_SPEECH_KEY",
      "Region": "eastus",
      "RecognitionLanguage": "en-US",
      "EndpointId": "OPTIONAL_CUSTOM_SPEECH_ENDPOINT_ID"
    }
  }
}
```

---

## Prerequisites

- Azure CLI installed and authenticated
- Domain with DNS control (for A records)
- SSL certificate for your domain (wildcard recommended)
- RDP client (for Windows VM access)

---

## Azure Infrastructure Setup

### Deploy VM and Resources

```bash
cd scripts
./deploy-azure-vm.sh
```

Creates:
- Resource Group: `rg-teams-media-bot-poc`
- App Registration with Calls.AccessMedia.All + Calls.JoinGroupCall.All permissions
- Azure Bot with Teams channel
- Azure Speech Service
- Windows Server 2022 VM (D4s_v3) with ports 443, 8445, 3389 open

Note VM credentials output at end of script.

### Verify Infrastructure

```bash
az resource list --resource-group rg-teams-media-bot-poc -o table
az vm show --name vm-tbot-prod --resource-group rg-teams-media-bot-poc -d
```

---

## Domain & SSL Setup

### Create DNS Records

Create two A records pointing to your VM's public IP:

```dns
teamsbot.yourdomain.com  A  <VM_PUBLIC_IP>
media.yourdomain.com     A  <VM_PUBLIC_IP>
```

Verify propagation:

```bash
nslookup teamsbot.yourdomain.com
nslookup media.yourdomain.com
```

### Install SSL Certificate on VM

RDP to VM, then in PowerShell as Administrator:

```powershell
# Import PFX
$password = ConvertTo-SecureString -String "YOUR_PFX_PASSWORD" -Force -AsPlainText
Import-PfxCertificate -FilePath "C:\certs\yourdomain.pfx" -CertStoreLocation Cert:\LocalMachine\My -Password $password

# Get thumbprint
Get-ChildItem Cert:\LocalMachine\My | Where-Object { $_.Subject -like "*yourdomain*" } | Select Thumbprint, Subject
```

Note the thumbprint for configuration.

---

## Build & Deploy

### Clone and Build

RDP to VM:

```
IP: <VM_PUBLIC_IP>
User: azureuser
Pass: <FROM_DEPLOYMENT_OUTPUT>
```

In PowerShell as Administrator:

```powershell
cd C:\
git clone https://github.com/your-org/teams-bot-poc.git
cd C:\teams-bot-poc\src

dotnet restore
dotnet build --configuration Release

# Verify NativeMedia.dll copied
Test-Path "bin\Release\net8.0\NativeMedia.dll"
```

### Configure appsettings.json

Update `C:\teams-bot-poc\src\Config\appsettings.json`:

```json
{
  "Bot": {
    "TenantId": "YOUR_TENANT_ID",
    "AppId": "YOUR_APP_ID",
    "AppSecret": "YOUR_APP_SECRET",
    "NotificationUrl": "https://teamsbot.yourdomain.com/api/calling",
    "LocalHttpListenUrl": "https://0.0.0.0:9443",
    "LocalHttpListenPort": 9443
  },
  "Stt": {
    "Provider": "AzureSpeech"
  },
  "MediaPlatformSettings": {
    "ApplicationId": "YOUR_APP_ID",
    "CertificateThumbprint": "YOUR_CERT_THUMBPRINT",
    "InstanceInternalPort": 8445,
    "InstancePublicPort": 8445,
    "ServiceFqdn": "media.yourdomain.com",
    "InstancePublicIPAddress": "0.0.0.0"
  },
  "Speech": {
    "Key": "YOUR_SPEECH_KEY",
    "Region": "eastus",
    "RecognitionLanguage": "en-US"
  },
  "TranscriptSink": {
    "PythonEndpoint": "http://127.0.0.1:8765/transcript"
  }
}
```

### Create Windows Service

```powershell
# Create logs directory
New-Item -ItemType Directory -Path C:\teams-bot-poc\logs -Force

# Install service with NSSM
nssm install TeamsMediaBot "C:\Program Files\dotnet\dotnet.exe"
nssm set TeamsMediaBot AppParameters "exec C:\teams-bot-poc\src\bin\Release\net8.0\TeamsMediaBot.dll"
nssm set TeamsMediaBot AppDirectory "C:\teams-bot-poc\src"
nssm set TeamsMediaBot ObjectName ".\azureuser" "YOUR_VM_PASSWORD"
nssm set TeamsMediaBot Start SERVICE_AUTO_START
nssm set TeamsMediaBot AppStdout "C:\teams-bot-poc\logs\service-output.log"
nssm set TeamsMediaBot AppStderr "C:\teams-bot-poc\logs\service-error.log"

# Start service
Start-Service TeamsMediaBot
Get-Service TeamsMediaBot
```

### Update Azure Bot Webhook

```bash
az bot update \
  --resource-group rg-teams-media-bot-poc \
  --name teams-media-bot-poc \
  --endpoint "https://teamsbot.yourdomain.com/api/calling"
```

---

## Teams App Package

### Update Manifest

Edit `manifest/manifest.json`:

```json
{
  "bots": [{
    "botId": "YOUR_APP_ID",
    "supportsCallingCapabilities": true,
    "supportsVideoCapabilities": true
  }]
}
```

### Create Package

```bash
cd manifest
zip -r teams-bot-poc.zip manifest.json color.png outline.png
```

### Upload to Teams

1. Teams Admin Center → Apps → Upload custom app
2. Select `teams-bot-poc.zip`
3. Approve permissions if prompted

---

## Testing

### Health Check

```bash
curl https://teamsbot.yourdomain.com/api/calling/health
```

Expected: `{"Status":"Healthy","Timestamp":"...","Service":"Teams Media Bot POC"}`

### Join Meeting

```bash
curl -X POST https://teamsbot.yourdomain.com/api/calling/join \
  -H "Content-Type: application/json" \
  -d '{"joinUrl":"TEAMS_MEETING_JOIN_URL","displayName":"Transcription Bot"}'
```

Bot should join within 5-10 seconds.

### Check Logs

On VM:

```powershell
# Service logs
Get-Content C:\teams-bot-poc\logs\service-output.log -Tail 50

# Audio processing
Get-Content C:\teams-bot-poc\logs\service-output.log | Select-String "Audio stats"

# Errors
Get-Content C:\teams-bot-poc\logs\service-error.log -Tail 20
```

---

## Python Transcript Service

### Setup

On VM or separate server:

```bash
cd python
pip install -r requirements.txt
python transcript_sink.py
```

Runs FastAPI server on port 8765. Bot POSTs transcripts to `http://127.0.0.1:8765/transcript` (configurable via `TranscriptSink.PythonEndpoint`).

### Integration

Modify `transcript_sink.py` to connect your agent:

```python
@app.post("/transcript")
async def receive_transcript(event: TranscriptEvent):
    await transcript_queue.put(event.dict())
    # Your agent logic here
    return {"status": "ok"}
```

---

## Debugging Commands

### Service Management

```powershell
# Check status
Get-Service TeamsMediaBot

# Restart
Restart-Service TeamsMediaBot

# Stop/Start
Stop-Service TeamsMediaBot
Start-Service TeamsMediaBot

# View config
nssm get TeamsMediaBot ObjectName
nssm get TeamsMediaBot AppDirectory
```

### Remote Diagnostics (Azure CLI)

```bash
# Get logs
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-tbot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-Content C:\teams-bot-poc\logs\service-output.log -Tail 100"

# Check service
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-tbot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-Service TeamsMediaBot | Select Status, StartType"
```

### Update Deployed Code

```bash
# On VM
cd C:\teams-bot-poc
git pull origin main
cd src
dotnet build --configuration Release
Restart-Service TeamsMediaBot
```

Or remotely:

```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-tbot-prod \
  --command-id RunPowerShellScript \
  --scripts @scripts/update-bot.ps1
```

---

## Architecture

```
Teams Meeting
    ↓
[Bot Signaling: port 443]  ← Azure Bot webhook
    ↓
[Ingress / TLS termination: :443 → Bot listener :9443]
    ↓
[Media Stream: port 8445]
    ↓
[Azure Speech Service]
    ↓
[Python Service: port 8765] ← Your agent framework
```

**Key Components:**
- `TeamsCallingBotService.cs` - Handles Teams Graph calls
- `AzureSpeechRealtimeTranscriber.cs` - Real-time speech-to-text
- `PythonTranscriptPublisher.cs` - HTTP POST to Python service
- `CallingController.cs` - Webhook endpoints

**Dependencies:**
- `Microsoft.Skype.Bots.Media` 1.31.0.225-preview (Windows Server 2022 compatible)
- `Microsoft.Graph.Communications.*` 1.2.0.15690
- Azure Speech SDK 1.40.*

---

## Reference


### Costs

- VM (D4s_v3): ~$140/month (region/pricing dependent)
- Speech Service (Pay-as-you-go): ~$1-25/month depending on usage
- Azure Bot: Free (Standard channel)
- Total: ~$141-165/month depending on Speech usage

Stop VM when not in use to reduce costs.

### Project Structure

```
teams-bot-poc/
├── src/
│   ├── TeamsMediaBot.csproj
│   ├── Program.cs
│   ├── Config/appsettings.json
│   ├── Controllers/CallingController.cs
│   └── Services/
│       ├── TeamsCallingBotService.cs
│       ├── AzureSpeechRealtimeTranscriber.cs
│       ├── PythonTranscriptPublisher.cs
│       ├── CallHandler.cs
│       └── HeartbeatHandler.cs
├── python/
│   ├── transcript_sink.py
│   └── requirements.txt
├── scripts/
│   ├── deploy-azure-vm.sh
│   ├── deploy-production.ps1
│   ├── update-bot.ps1
│   └── diagnose-bot.ps1
├── manifest/
│   ├── manifest.json
│   ├── color.png
│   └── outline.png
└── README.md
```

### Critical Configuration Notes

**Service Account:** Must run as `.\azureuser` for certificate access

**Package Versions:** Do not upgrade `Microsoft.Skype.Bots.Media` beyond 1.31.0.225-preview - version 1.32.x causes "Procedure Not Found" errors on Windows Server 2022

**Native Dependencies:** Verify `NativeMedia.dll` exists in output directory after build

**Firewall:** NSG rules must allow inbound 443 (signaling) and 8445 (media)

---

## Common Issues

**Bot won't start:**
- Check certificate thumbprint matches installed cert
- Verify port 443 not in use: `Get-NetTCPConnection -LocalPort 443`
- Check service account: `nssm get TeamsMediaBot ObjectName`

**Bot joins but no audio:**
- Verify DNS: `nslookup media.yourdomain.com`
- Check media port: `Get-NetTCPConnection -LocalPort 8445`
- Review logs for media connection errors

**No transcripts:**
- Verify audio frames received: `Get-Content logs\service-output.log | Select-String "Audio stats"`
- Check Speech Service key and region in appsettings.json
- Test Speech connectivity: `Test-NetConnection eastus.api.cognitive.microsoft.com -Port 443`

**Deployment updates not applying:**
- Ensure service restarted after code changes
- Check service running: `Get-Service TeamsMediaBot`
- Verify correct working directory: `nssm get TeamsMediaBot AppDirectory`
