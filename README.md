# Talestral by Talestry

AI-powered meeting transcription bot that joins Teams meetings, provides real-time diarized audio transcription with speaker identification, and streams speaker-attributed transcripts to a Python agent endpoint for interview analysis.

**Status:** Deployed and operational on Azure Windows VM  
**Domain:** `teamsbot.qmachina.com` / `media.qmachina.com`  
**VM:** `52.188.117.153` (Windows Server 2022, D4s_v3)

**Implementation Status:** ✅ COMPLETE (Python: ✅ | C#: ✅)

## Key Features (v2)

- **Diarized Transcription**: Identifies who is speaking (`speaker_0`, `speaker_1`, etc.)
- **Provider Choice**: Deepgram (primary, best diarization) or Azure ConversationTranscriber (fallback)
- **Real-Time Streaming**: ~100-300ms latency to Python endpoint
- **Interview Analysis Agent**: OpenAI Agents SDK-based analyzer scores candidate responses
- **External Deployment**: Python agent at `agent.qmachina.com` (separate from C# bot)

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
│  │  │ TranscriberFactory → IRealtimeTranscriber                │  │     │
│  │  │  • DeepgramRealtimeTranscriber (PRIMARY - best diarization)│ │     │
│  │  │  • AzureConversationTranscriber (FALLBACK - enterprise) │  │     │
│  │  │  • Emits diarized transcripts with speaker IDs           │  │     │
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
                                                     wss:// (STT APIs)
                        ┌────────────────────────────┐  ┌────────────────────────────┐
                        │  Deepgram API (PRIMARY)     │  │  Azure Speech Service      │
                        │  • Real-time diarization    │  │  (speech-teams-bot-poc)    │
                        │  • Speaker IDs per word     │  │  Region: eastus            │
                        └────────────────────────────┘  └────────────────────────────┘


Current Trigger Method:
  curl -X POST https://teamsbot.qmachina.com/api/calling/join \
    -H "Content-Type: application/json" \
    -d '{"joinUrl":"TEAMS_MEETING_URL","displayName":"Talestral by Talestry"}'
```

### Ingress / :443 forwarding (what the code expects)

The repo’s current configuration binds the bot’s **Kestrel backend to `https://0.0.0.0:9443`** (see `src/Config/appsettings.json`). Because the webhook URL is `https://teamsbot.qmachina.com/...` and **:443** is exposed publicly, you need a **reverse proxy on the VM** to accept TLS on :443 and forward traffic to the local bot backend on :9443.

Implementation-wise this can be IIS, nginx, Caddy, or any other layer that performs **:443 → :9443** forwarding. The important point is: **the bot process itself is not listening on :443 in the current config**.

### STT provider/model selection (no fan-out)

- **Source of truth**: the C# code creates **one transcriber per call** and pushes Teams PCM frames into it.
- **Choosing provider**: STT selection is config-driven via `Stt.Provider` (supports `Deepgram` or `AzureSpeech`).
- **Deepgram (recommended)**: Best-in-class diarization with speaker IDs on every word. Model selection via `Stt.Deepgram.Model` (default: "nova-3").
- **Azure ConversationTranscriber (fallback)**: Enterprise-grade with real-time diarization. Supports Custom Speech models via `Stt.AzureSpeech.EndpointId` (optional).
- **No fan-out**: the bot does **not** stream the same audio to multiple STT providers/models.

Example configuration (Deepgram as primary):

```json
{
  "Stt": {
    "Provider": "Deepgram",
    "Deepgram": {
      "ApiKey": "YOUR_DEEPGRAM_API_KEY",
      "Model": "nova-3",
      "Diarize": true
    },
    "AzureSpeech": {
      "Key": "YOUR_SPEECH_KEY",
      "Region": "eastus",
      "RecognitionLanguage": "en-US",
      "EndpointId": null
    }
  }
}
```

Example configuration (Azure Speech as fallback):

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
    "Provider": "Deepgram",
    "Deepgram": {
      "ApiKey": "YOUR_DEEPGRAM_API_KEY",
      "Model": "nova-3",
      "Diarize": true
    },
    "AzureSpeech": {
      "Key": "YOUR_AZURE_SPEECH_KEY",
      "Region": "eastus",
      "RecognitionLanguage": "en-US",
      "EndpointId": null
    }
  },
  "MediaPlatformSettings": {
    "ApplicationId": "YOUR_APP_ID",
    "CertificateThumbprint": "YOUR_CERT_THUMBPRINT",
    "InstanceInternalPort": 8445,
    "InstancePublicPort": 8445,
    "ServiceFqdn": "media.yourdomain.com",
    "InstancePublicIPAddress": "0.0.0.0"
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

Expected: `{"Status":"Healthy","Timestamp":"...","Service":"Talestral by Talestry"}`

### Join Meeting

```bash
curl -X POST https://teamsbot.yourdomain.com/api/calling/join \
  -H "Content-Type: application/json" \
  -d '{"joinUrl":"TEAMS_MEETING_JOIN_URL","displayName":"Talestral by Talestry"}'
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
uv venv
uv pip install -r requirements.txt
uv run transcript_sink.py
```

Runs FastAPI server on `http://0.0.0.0:8765`. Target deployment: `https://agent.qmachina.com` (behind TLS proxy).

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/transcript` | POST | Receive transcript events (v1 and v2 format) |
| `/session/start` | POST | Start interview session with candidate_name, meeting_url |
| `/session/map-speaker` | POST | Map speaker_id to role (candidate/interviewer) |
| `/session/status` | GET | Get current session info |
| `/session/end` | POST | End session, finalize analysis |
| `/health` | GET | Health check |
| `/stats` | GET | Statistics |

### Session Workflow

```bash
# 1. Start session before meeting
curl -X POST http://localhost:8765/session/start \
  -H "Content-Type: application/json" \
  -d '{"candidate_name":"John Doe","meeting_url":"https://teams.microsoft.com/..."}'

# 2. Map speakers as they are identified by diarization
curl -X POST http://localhost:8765/session/map-speaker \
  -H "Content-Type: application/json" \
  -d '{"speaker_id":"speaker_0","role":"interviewer"}'

curl -X POST http://localhost:8765/session/map-speaker \
  -H "Content-Type: application/json" \
  -d '{"speaker_id":"speaker_1","role":"candidate"}'

# 3. Check session status
curl http://localhost:8765/session/status

# 4. End session after interview
curl -X POST http://localhost:8765/session/end
```

### Agent Integration (OpenAI Agents SDK)

The sink integrates with `interview_agent` package:
- `interview_agent.models.TranscriptEvent` - v2 transcript format with diarization
- `interview_agent.session.InterviewSessionManager` - session state management
- `interview_agent.output.AnalysisOutputWriter` - analysis JSON output to `python/output/`
- `interview_agent.agent.InterviewAnalyzer` - OpenAI Agents SDK interview analyzer

**Real-time analysis is enabled via `interview_agent/agent.py`:**

```python
from interview_agent import InterviewAnalyzer, create_interview_analyzer

# Initialize analyzer (requires OPENAI_API_KEY env var)
analyzer = create_interview_analyzer(model="gpt-4o", output_dir="./output")

# Analyze a candidate response
result = await analyzer.analyze_async(
    response_text="I have 5 years of Python experience...",
    context={"candidate_name": "John Doe", "conversation_history": [...]}
)

print(result.relevance_score)  # 0.85
print(result.clarity_score)    # 0.90
print(result.key_points)       # ["5 years experience", "Backend development"]
```

**Environment Setup:**
```bash
export OPENAI_API_KEY="sk-..."
```

### Output

- Transcripts: `~/Desktop/meeting_transcript.txt` (Windows VM)
- Analysis JSON: `python/output/{session_id}_analysis.json`

### Testing

Run the test suite:

```bash
cd python
uv run pytest tests/ -v
```

**Test Modules:**
- `tests/mock_data.py` - Mock data generators for TranscriptEvent, AnalysisItem, interview conversations
- `tests/test_interview.py` - Tests for InterviewSessionManager, AnalysisOutputWriter, models
- `tests/test_sink.py` - FastAPI endpoint tests for transcript_sink.py

**Mock Data Functions:**
- `generate_session_start_event()` / `generate_session_stop_event()` - Session lifecycle events
- `generate_transcript_event(speaker_id, text, event_type)` - Single transcript event
- `generate_interview_conversation(candidate_name, num_exchanges)` - Full interview simulation
- `generate_v2_event_dict()` / `generate_v1_event_dict()` - Raw JSON format matching C# bot output

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
- `TranscriberFactory.cs` - Creates provider-specific transcribers
- `DeepgramRealtimeTranscriber.cs` - Primary STT provider (best diarization)
- `AzureConversationTranscriber.cs` - Fallback STT provider (enterprise-grade diarization)
- `PythonTranscriptPublisher.cs` - HTTP POST to Python service (snake_case JSON)
- `CallingController.cs` - Webhook endpoints

**Dependencies:**
- `Microsoft.Skype.Bots.Media` 1.31.0.225-preview (Windows Server 2022 compatible)
- `Microsoft.Graph.Communications.*` 1.2.0.15690
- Azure Speech SDK 1.40.* (for ConversationTranscriber)
- Deepgram SDK 3.0.0 (for primary transcription)

---

## Reference

### Project Structure

```
teams-bot-poc/
├── src/
│   ├── TeamsMediaBot.csproj
│   ├── Program.cs
│   ├── Config/appsettings.json
│   ├── Controllers/CallingController.cs
│   ├── Models/
│   │   ├── TranscriptEvent.cs (v2 with diarization support)
│   │   └── BotConfiguration.cs
│   └── Services/
│       ├── TeamsCallingBotService.cs
│       ├── TranscriberFactory.cs
│       ├── DeepgramRealtimeTranscriber.cs (PRIMARY)
│       ├── AzureConversationTranscriber.cs (FALLBACK)
│       ├── AzureSpeechRealtimeTranscriber.cs (DEPRECATED - no diarization)
│       ├── PythonTranscriptPublisher.cs
│       ├── CallHandler.cs
│       └── HeartbeatHandler.cs
├── python/
│   ├── pyproject.toml              # uv project config
│   ├── transcript_sink.py          # FastAPI transcript receiver
│   ├── simulate_interview.py       # CLI interview simulator
│   ├── streamlit_ui.py             # Modern three-column UI with built-in simulation
│   ├── interview_agent/            # Interview analysis agent package
│   │   ├── __init__.py             # Package exports (InterviewAnalyzer, etc.)
│   │   ├── agent.py                # OpenAI Agents SDK interview analyzer
│   │   ├── models.py               # Pydantic models (TranscriptEvent, AnalysisItem, etc.)
│   │   ├── session.py              # InterviewSessionManager
│   │   └── output.py               # AnalysisOutputWriter (JSON persistence)
│   └── tests/                      # Test suite
│       ├── __init__.py
│       ├── mock_data.py            # Mock data generators for testing
│       ├── test_interview.py       # InterviewSessionManager, AnalysisOutputWriter tests
│       └── test_sink.py            # FastAPI endpoint tests
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

### Interview Analysis Agent

The `interview_agent` package uses the OpenAI Agents SDK to analyze candidate responses in real-time.

**Setup:**
```bash
cd python
uv sync  # Install dependencies
export OPENAI_API_KEY=sk-...
```

**Usage in transcript_sink:**
```python
from interview_agent import InterviewAnalyzer

analyzer = InterviewAnalyzer(model="gpt-4o")
result = await analyzer.analyze_async(
    response_text="I have 5 years of Python experience...",
    context={
        "candidate_name": "John Smith",
        "conversation_history": [
            {"role": "interviewer", "text": "Tell me about your Python experience."}
        ]
    }
)
print(f"Relevance: {result.relevance_score}, Clarity: {result.clarity_score}")
```

**Analysis Output:**
- `relevance_score` (0-1): How relevant the response is to the question
- `clarity_score` (0-1): How clearly the response is articulated
- `key_points`: Extracted key points from the response
- `follow_up_suggestions`: Suggested follow-up questions

### Streamlit UI

Modern three-column Streamlit interface with built-in interview simulation.

**Launch:**
```bash
cd python
uv run streamlit run streamlit_ui.py
```

**Layout:**
| Column | Width | Contents |
|--------|-------|----------|
| Left | 20% | Meeting ID, participants, session stats |
| Center | 50% | Chat-style transcript + agent analysis bubbles |
| Right | 30% | Interview checklist with stoplight indicators |

**Simulation Controls:**
| Button | Action |
|--------|--------|
| ▶️ Simulate | Start 20-message interview simulation |
| ⏹️ Stop | Pause simulation at current position |
| 🔄 Restart | Reset and start fresh |

**Checklist Items:**
- Intro, Role Overview, Background, Python Question, Salary Expectations, Next Steps
- Stoplight indicators: ⚪ pending, 🟡 analyzing, 🟢 complete
- Auto-detection via keyword matching in transcripts

**State Management (st.session_state):**
- `simulation_running`: bool
- `simulation_index`: int (0-19)
- `messages`: list of ChatMessage objects
- `checklist_status`: dict mapping item_id to status

### Critical Configuration Notes

**Service Account:** Must run as `.\azureuser` for certificate access

**Package Versions:** Do not upgrade `Microsoft.Skype.Bots.Media` beyond 1.31.0.225-preview - version 1.32.x causes "Procedure Not Found" errors on Windows Server 2022

**Native Dependencies:** Verify `NativeMedia.dll` exists in output directory after build

**Firewall:** NSG rules must allow inbound 443 (signaling) and 8445 (media)

---

## Azure Agent Deployment (FastAPI + Streamlit)

The Python agent (FastAPI transcript sink + Streamlit UI) can be deployed to Azure Container Apps with Azure OpenAI backend.

### Deploy to Azure

```bash
cd scripts
./deploy-azure-agent.sh
```

Creates:
- Azure OpenAI resource with gpt-5 deployment (low reasoning effort for fast real-time analysis)
- Container Apps Environment (serverless, scale-to-zero)
- FastAPI container app (transcript sink)
- Streamlit container app (interview analysis UI)

**Estimated Cost:** ~$10-50/month (POC, usage-based, GPT-5)

### GoDaddy DNS Setup

After running the deployment script, create a CNAME record in GoDaddy:

1. Go to [GoDaddy DNS Management](https://dcc.godaddy.com/manage/dns)
2. Select your domain (e.g., qmachina.com)
3. Add a CNAME record:

| Type | Name | Value | TTL |
|------|------|-------|-----|
| CNAME | agent | ca-talestral-api.*.azurecontainerapps.io | 600 |

For Streamlit UI (optional separate subdomain):

| Type | Name | Value | TTL |
|------|------|-------|-----|
| CNAME | interview | ca-talestral-ui.*.azurecontainerapps.io | 600 |

### Bind Custom Domain (After DNS Propagates)

```bash
# Verify DNS propagation (wait 5-15 minutes)
nslookup agent.qmachina.com

# Add custom domain to FastAPI app
az containerapp hostname add \
  --name ca-talestral-api \
  --resource-group rg-teams-media-bot-poc \
  --hostname agent.qmachina.com

# Bind managed certificate (automatic TLS)
az containerapp hostname bind \
  --name ca-talestral-api \
  --resource-group rg-teams-media-bot-poc \
  --hostname agent.qmachina.com \
  --environment cae-talestral-poc \
  --validation-method CNAME
```

### Update C# Bot Configuration

After deploying the agent, update the Windows VM bot to send transcripts to Azure:

```json
{
  "TranscriptSink": {
    "PythonEndpoint": "https://agent.qmachina.com/transcript"
  }
}
```

Then restart the service: `Restart-Service TeamsMediaBot`

### Azure OpenAI Configuration

The agent automatically detects Azure OpenAI when these environment variables are set:

| Variable | Description |
|----------|-------------|
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI endpoint URL |
| `AZURE_OPENAI_KEY` | Azure OpenAI API key |
| `AZURE_OPENAI_DEPLOYMENT` | Model deployment name (e.g., gpt-5) |
| `OPENAI_REASONING_EFFORT` | GPT-5 reasoning effort: low, medium, high (default: low) |
| `AZURE_OPENAI_API_VERSION` | API version (default: 2024-08-01-preview) |
| `OPENAI_API_TYPE` | Set to "azure" to force Azure mode |

For standard OpenAI (local development), set `OPENAI_API_KEY` instead.

### Test Deployment

```bash
# Health check
curl https://agent.qmachina.com/health

# Stats
curl https://agent.qmachina.com/stats

# Streamlit UI
open https://interview.qmachina.com
```

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
