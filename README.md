# Talestral

AI-powered meeting transcription bot that joins Teams meetings, provides real-time diarized audio transcription with speaker identification, and streams speaker-attributed transcripts to a Python agent endpoint for interview analysis.

**Status:** Deployed and operational on Azure Windows VM  
**Domain:** `teamsbot.qmachina.com` / `media.qmachina.com`  
**VM:** `52.188.117.153` (Windows Server 2022, D4s_v3)

**Implementation Status:** ✅ COMPLETE (Python: ✅ | C#: ✅)  
**Active STT Provider:** Deepgram (nova-3, diarize=true, endpointing=10)  
**Last Build:** Feb 5, 2026 -- 0 errors, 0 warnings

## Key Features (v2)

- **Diarized Transcription**: Identifies who is speaking (`speaker_0`, `speaker_1`, etc.)
- **Provider Choice**: Deepgram (primary, best diarization) or Azure ConversationTranscriber (fallback)
- **Real-Time Streaming**: ~100-300ms latency to Python endpoint
- **Interview Analysis Agent**: OpenAI Agents SDK-based analyzer scores candidate responses
- **External Deployment**: Python agent at `agent.qmachina.com` (separate from C# bot)
- **Multi-Instance Stacks**: Run multiple isolated `bot -> sink -> UI` pipelines concurrently with per-instance config and variant launchers

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
│  │  │  • POST → TranscriptSink.PythonEndpoint (per instance)   │  │     │
│  │  │  • Also saves: Desktop\\meeting_transcript.txt           │  │     │
│  │  └─────────────────────────────────────────────────────────┘  │     │
│  └───────────────────────────────────────────────────────────────┘     │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────┐     │
│  │ Python Transcript Sink (optional, same VM or elsewhere)        │     │
│  │  • `python/run_variant_sink.py` + `python/transcript_sink.py`  │     │
│  │  • Variant+instance-aware, POST /transcript on configured port │     │
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
    -d '{"joinUrl":"TEAMS_MEETING_URL","displayName":"Talestral"}'
```

### Ingress / :443 forwarding (what the code expects)

The repo’s current configuration binds the bot’s **Kestrel backend to `https://0.0.0.0:9443`** (see `src/Config/appsettings.json`). Because the webhook URL is `https://teamsbot.qmachina.com/...` and **:443** is exposed publicly, you need a **reverse proxy on the VM** to accept TLS on :443 and forward traffic to the local bot backend on :9443.

Implementation-wise this can be IIS, nginx, Caddy, or any other layer that performs **:443 → :9443** forwarding. The important point is: **the bot process itself is not listening on :443 in the current config**.

### STT provider/model selection (no fan-out)

- **Source of truth**: the C# code creates **one transcriber per call** and pushes Teams PCM frames into it.
- **Choosing provider**: STT selection is config-driven via `Stt.Provider` (supports `Deepgram` or `AzureSpeech`).
- **Deepgram (recommended, active)**: Best-in-class diarization with speaker IDs on every word. Model selection via `Stt.Deepgram.Model` (default: "nova-3"). WebSocket parameters sent: `diarize=true, endpointing=10, interim_results=true, smart_format=true, punctuate=true, utterance_end_ms=1000, encoding=linear16, sample_rate=16000`.
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

### Configure appsettings (single or multi-instance)

For single-instance, update `C:\teams-bot-poc\src\Config\appsettings.json`.

For multi-instance, create one file per bot service (examples in repo):
- `src/Config/appsettings.instance-a.example.json`
- `src/Config/appsettings.instance-b.example.json`

Each instance file must have its own listen/media ports and its own `TranscriptSink.PythonEndpoint`.

Example instance config:

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

Manual launch examples (from `src/bin/Release/net8.0`):

```powershell
.\TeamsMediaBot.exe --config C:\teams-bot-poc\src\Config\appsettings.instance-a.json
.\TeamsMediaBot.exe --config C:\teams-bot-poc\src\Config\appsettings.instance-b.json
```

### Create Windows Service

```powershell
# Create logs directory
New-Item -ItemType Directory -Path C:\teams-bot-poc\logs -Force

# Install service with NSSM (single instance example)
nssm install TeamsMediaBot "C:\teams-bot-poc\src\bin\Release\net8.0\TeamsMediaBot.exe"
nssm set TeamsMediaBot AppDirectory "C:\teams-bot-poc\src\bin\Release\net8.0"
nssm set TeamsMediaBot AppParameters "--config C:\teams-bot-poc\src\Config\appsettings.json"
nssm set TeamsMediaBot ObjectName ".\azureuser" "YOUR_VM_PASSWORD"
nssm set TeamsMediaBot Start SERVICE_AUTO_START
nssm set TeamsMediaBot AppStdout "C:\teams-bot-poc\logs\service-output.log"
nssm set TeamsMediaBot AppStderr "C:\teams-bot-poc\logs\service-error.log"

# Start service
Start-Service TeamsMediaBot
Get-Service TeamsMediaBot
```

> **Note:** If the service fails to start with "logon failure", update the password:
> `nssm set TeamsMediaBot ObjectName ".\azureuser" "NEW_PASSWORD"`

For multi-instance on one host, create unique service names with unique config paths:

```powershell
nssm install TeamsMediaBot-A "C:\teams-bot-poc\src\bin\Release\net8.0\TeamsMediaBot.exe"
nssm set TeamsMediaBot-A AppDirectory "C:\teams-bot-poc\src\bin\Release\net8.0"
nssm set TeamsMediaBot-A AppParameters "--config C:\teams-bot-poc\src\Config\appsettings.instance-a.json"

nssm install TeamsMediaBot-B "C:\teams-bot-poc\src\bin\Release\net8.0\TeamsMediaBot.exe"
nssm set TeamsMediaBot-B AppDirectory "C:\teams-bot-poc\src\bin\Release\net8.0"
nssm set TeamsMediaBot-B AppParameters "--config C:\teams-bot-poc\src\Config\appsettings.instance-b.json"
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
curl https://teamsbot.qmachina.com/api/calling/health
```

Expected: `{"status":"Healthy","timestampUtc":"...","service":"Talestral","activeCalls":0}`

### Join a Teams Meeting

To add the bot to a live Teams meeting, copy the meeting join URL from Teams and run:

```bash
curl -X POST https://teamsbot.qmachina.com/api/calling/join \
  -H "Content-Type: application/json" \
  -d '{"joinUrl":"PASTE_TEAMS_MEETING_JOIN_URL_HERE","displayName":"Talestral"}'
```

On Windows (PowerShell):

```powershell
Invoke-WebRequest -Uri "https://teamsbot.qmachina.com/api/calling/join" `
  -Method POST `
  -ContentType "application/json" `
  -Body '{"joinUrl":"PASTE_TEAMS_MEETING_JOIN_URL_HERE","displayName":"Talestral"}'
```

The bot should join within 5-10 seconds. You'll see "Talestral" appear as a participant.

**Join URL formats supported:**
- New: `https://teams.microsoft.com/meet/{meetingId}?p={passcode}`
- Legacy: `https://teams.microsoft.com/l/meetup-join/{thread}/{message}?context={...}`

**Optional parameters:**
- `displayName` (string, default: "Talestral") - Bot's display name in the meeting
- `joinAsGuest` (bool, default: false) - Join as guest identity with the display name

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

## Python Components

All Python components live in the `python/` directory. Shared prerequisite:

```bash
cd python
uv sync  # Install dependencies from pyproject.toml
```

Environment: `python/.env` contains `OPENAI_API_KEY=sk-...` (loaded automatically by the agent). For Azure OpenAI deployment, set the Azure env vars instead (see Azure Agent Deployment section).

### FastAPI Transcript Sink (`transcript_sink.py`, `run_variant_sink.py`)

Receives transcript events from the C# bot (or from the Streamlit simulation) and triggers real-time LLM analysis via the Interview Analysis Agent.

**Launch (single-instance compatibility):**
```bash
cd python
uv run python transcript_sink.py
# Listens on http://0.0.0.0:8765
```

**Launch (multi-instance, recommended):**
```bash
cd python
uv run python run_variant_sink.py --variant default --instance meeting-a --port 8765
uv run python run_variant_sink.py --variant behavioral --instance meeting-b --port 8865
```

`run_variant_sink.py` sets:
- `VARIANT_ID` (`default`, `behavioral`)
- `INSTANCE_ID`
- `SINK_HOST` / `SINK_PORT`
- `OUTPUT_DIR` (defaults to `python/output/<instance>`)

**Endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/transcript` | POST | Receive transcript events (v1 and v2 format) |
| `/session/start` | POST | Start interview session with candidate_name, meeting_url |
| `/session/map-speaker` | POST | Map speaker_id to role (candidate/interviewer) |
| `/session/status` | GET | Get current session info |
| `/session/end` | POST | End session, finalize analysis |
| `/health` | GET | Health check |
| `/stats` | GET | Statistics |

**Session Workflow:**

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

**Output:**
- Transcripts: `~/Desktop/meeting_transcript.txt` (default instance) or `~/Desktop/meeting_transcript_<instance>.txt`
- Analysis JSON: `python/output/{session_id}_analysis.json` (default) or `python/output/<instance>/{session_id}_analysis.json`

### Interview Analysis Agent (`interview_agent/`)

OpenAI Agents SDK package that scores candidate responses in real-time. Called by the FastAPI sink when candidate transcript events arrive.

**Package modules:**
- `interview_agent.agent` - `InterviewAnalyzer` / `create_interview_analyzer` (OpenAI Agents SDK)
- `interview_agent.models` - Pydantic models (`TranscriptEvent`, `AnalysisItem`, etc.)
- `interview_agent.session` - `InterviewSessionManager` (session state management)
- `interview_agent.output` - `AnalysisOutputWriter` (JSON persistence to `python/output/`)

**Programmatic usage:**
```python
from interview_agent import InterviewAnalyzer, create_interview_analyzer

analyzer = create_interview_analyzer(model="gpt-4o", output_dir="./output")
result = await analyzer.analyze_async(
    response_text="I have 5 years of Python experience...",
    context={"candidate_name": "John Doe", "conversation_history": [...]}
)

print(result.relevance_score)  # 0-1: relevance to the question
print(result.clarity_score)    # 0-1: articulation clarity
print(result.key_points)       # extracted key observations
print(result.follow_up_suggestions)  # suggested follow-up questions
```

### Streamlit UI (`streamlit_ui.py`, `run_variant_ui.py`)

Three-column real-time interview display. Does NOT call the LLM directly — it sends transcript events to the FastAPI sink via HTTP and polls `python/output/` for analysis results.

**Launch (single-instance compatibility):**
```bash
cd python
uv run streamlit run streamlit_ui.py --server.port 8502
# Opens browser at http://localhost:8502
```

**Launch (multi-instance, recommended):**
```bash
cd python
uv run python run_variant_ui.py --variant default --instance meeting-a --port 8502 --sink-url http://127.0.0.1:8765
uv run python run_variant_ui.py --variant behavioral --instance meeting-b --port 8602 --sink-url http://127.0.0.1:8865
```

`streamlit_ui.py` now loads checklist/script/title from the active variant plugin.

**Requires the FastAPI sink to be running** (see above). The UI header shows a green/red connection indicator for sink status.

**UI Layout:**
| Column | Width | Contents |
|--------|-------|----------|
| Left | 20% | Meeting ID, participants, session stats |
| Center | 50% | Chat-style transcript + agent analysis coaching bubbles |
| Right | 30% | Interview checklist with stoplight progress indicators |

**Built-in simulation controls (in the header):**
| Button | Action |
|--------|--------|
| Simulate | Start variant-defined mock interview script |
| Stop | Pause simulation at current position |
| Restart | Reset and start fresh |

**Checklist items:** Variant-specific and keyword-driven (e.g., default vs behavioral). Stoplight indicators: pending, analyzing, complete.

### Testing

```bash
cd python
uv run pytest tests/ -v
```

**Test Modules:**
- `tests/mock_data.py` - Mock data generators for TranscriptEvent, AnalysisItem, interview conversations
- `tests/test_interview.py` - Tests for InterviewSessionManager, AnalysisOutputWriter, models
- `tests/test_sink.py` - FastAPI endpoint tests for transcript_sink.py
- `tests/test_variants.py` - Variant registry and plugin behavior tests

---

## Run Modes

### Demo Mode (local, no Teams meeting required)

Runs a scripted simulated interview through the full pipeline with **real LLM calls**. Requires **two processes simultaneously**:

```bash
# Terminal 1: FastAPI transcript sink (receives events, calls OpenAI for analysis)
cd python
uv run python transcript_sink.py

# Terminal 2: Streamlit UI (drives simulation, displays results)
cd python
uv run streamlit run streamlit_ui.py --server.port 8502
```

Then click "Simulate" in the Streamlit header. The data flow:

```
Streamlit UI (streamlit_ui.py)
    │
    │  HTTP POST simulated transcript events
    ↓
FastAPI Sink (transcript_sink.py :8765)
    │
    │  Calls InterviewAnalyzer (OpenAI Agents SDK)
    ↓
OpenAI API (real LLM calls, requires OPENAI_API_KEY in python/.env)
    │
    │  Analysis results written to python/output/{session_id}_analysis.json
    ↓
Streamlit UI (polls output/ directory, renders coaching bubbles)
```

### Multi-Instance Demo Mode (multiple meetings/agents/UI)

Run two isolated pipelines on one machine:

```bash
# Stack A
cd python
uv run python run_variant_sink.py --variant default --instance meeting-a --port 8765
# In another terminal
cd python
uv run python run_variant_ui.py --variant default --instance meeting-a --port 8502 --sink-url http://127.0.0.1:8765

# Stack B
cd python
uv run python run_variant_sink.py --variant behavioral --instance meeting-b --port 8865
# In another terminal
cd python
uv run python run_variant_ui.py --variant behavioral --instance meeting-b --port 8602 --sink-url http://127.0.0.1:8865
```

Each stack has:
- isolated FastAPI process state/session memory
- isolated output directory (`python/output/<instance>`)
- isolated transcript file (`meeting_transcript_<instance>.txt`)
- variant-specific UI/checklist and coaching context

### Live Mode (real Teams meeting)

The C# bot joins a Teams meeting, transcribes audio via Deepgram, and POSTs transcript events to the FastAPI sink. The Streamlit UI displays the live transcript and coaching analysis.

**Processes required:**
1. C# TeamsMediaBot Windows Service (on Azure VM, already deployed)
2. FastAPI transcript sink (on VM or `agent.qmachina.com`)
3. Streamlit UI (optional, for real-time monitoring)

**Trigger:**
```bash
curl -X POST https://teamsbot.qmachina.com/api/calling/join \
  -H "Content-Type: application/json" \
  -d '{"joinUrl":"PASTE_TEAMS_MEETING_JOIN_URL_HERE","displayName":"Talestral"}'
```

The data flow follows the full architecture diagram at the top of this file.

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
[Ingress / TLS termination: :443 → Bot listener :9443 (instance-specific)]
    ↓
[Media Stream: port 8445 (instance-specific)]
    ↓
[Deepgram / Azure Speech]  ← Config-driven via Stt.Provider
    ↓
[Python Service: configurable sink port] ← variant-aware agent framework
    ↓
[Streamlit UI: configurable port] ← variant-aware interview UI
```

**Key Components:**
- `TeamsCallingBotService.cs` - Handles Teams Graph calls
- `TranscriberFactory.cs` - Creates provider-specific transcribers (in `AzureSpeechRealtimeTranscriber.cs`)
- `DeepgramRealtimeTranscriber.cs` - Primary STT provider (best diarization, Deepgram SDK v6)
- `AzureConversationTranscriber.cs` - Fallback STT provider (enterprise-grade diarization)
- `AzureSpeechRealtimeTranscriber.cs` - Deprecated (no diarization, contains TranscriberFactory)
- `PythonTranscriptPublisher.cs` - HTTP POST to Python service (snake_case JSON)
- `CallingController.cs` - Webhook endpoints + join/health APIs
- `run_variant_sink.py` / `run_variant_ui.py` - Multi-instance launchers for variant stacks

**Dependencies:**
- `Microsoft.Skype.Bots.Media` 1.31.0.225-preview (Windows Server 2022 compatible)
- `Microsoft.Graph.Communications.*` 1.2.0.15690
- Azure Speech SDK 1.40.0 (for ConversationTranscriber)
- Deepgram SDK 6.6.1 (for primary transcription)
- `Serilog.Enrichers.Environment` 3.0.1 / `Serilog.Enrichers.Thread` 4.0.0

---

## Reference

### Project Structure

```
teams-bot-poc/
├── src/
│   ├── TeamsMediaBot.csproj
│   ├── Program.cs
│   ├── Config/appsettings.json       # Credentials + STT provider config (DO NOT commit)
│   ├── Controllers/CallingController.cs
│   ├── Models/
│   │   ├── TranscriptEvent.cs        # v2 event format with diarization support
│   │   └── BotConfiguration.cs       # Config models (Bot, STT, Media, TranscriptSink)
│   └── Services/
│       ├── TeamsCallingBotService.cs  # Graph Communications SDK, join/leave calls
│       ├── IRealtimeTranscriber.cs    # Transcriber interface (provider-agnostic)
│       ├── DeepgramRealtimeTranscriber.cs   # PRIMARY - Deepgram SDK v6 WebSocket streaming
│       ├── AzureConversationTranscriber.cs  # FALLBACK - Azure ConversationTranscriber
│       ├── AzureSpeechRealtimeTranscriber.cs # DEPRECATED (no diarization) + TranscriberFactory
│       ├── PythonTranscriptPublisher.cs     # HTTP POST to Python endpoint (snake_case JSON)
│       ├── CallHandler.cs            # Per-call lifecycle, audio forwarding, heartbeat
│       └── HeartbeatHandler.cs       # Base class for Graph API keepalive timer
├── DeepgramTest/                     # Standalone validation tool for Deepgram API key + connection
│   ├── DeepgramTest.csproj
│   └── Program.cs
├── LoaderTest/                       # SDK reflection inspector (development utility)
│   ├── LoaderTest.csproj
│   └── Program.cs
├── python/
│   ├── pyproject.toml                # uv project config
│   ├── transcript_sink.py            # FastAPI transcript receiver
│   ├── run_variant_sink.py           # Variant/instance sink launcher
│   ├── run_variant_ui.py             # Variant/instance Streamlit launcher
│   ├── simulate_interview.py         # CLI interview simulator
│   ├── streamlit_ui.py               # Modern three-column UI with built-in simulation
│   ├── variants/                     # Variant plugins (UI + agent behavior)
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── default.py
│   │   ├── behavioral.py
│   │   └── shared_content.py
│   ├── interview_agent/              # Interview analysis agent package
│   │   ├── __init__.py
│   │   ├── agent.py                  # OpenAI Agents SDK interview analyzer
│   │   ├── models.py                 # Pydantic models (TranscriptEvent, AnalysisItem, etc.)
│   │   ├── session.py                # InterviewSessionManager
│   │   └── output.py                 # AnalysisOutputWriter (JSON persistence)
│   └── tests/
│       ├── __init__.py
│       ├── mock_data.py
│       ├── test_interview.py
│       ├── test_sink.py
│       └── test_variants.py
├── scripts/
│   ├── deploy-azure-vm.sh
│   ├── deploy-azure-agent.sh
│   ├── deploy-production.ps1
│   ├── update-bot.ps1
│   └── diagnose-bot.ps1
├── manifest/
│   ├── manifest.json                 # Teams app manifest (bot ID, permissions)
│   ├── color.png
│   ├── outline.png
│   └── teams-bot-poc.zip             # Ready-to-upload Teams app package
└── README.md
```

### Critical Configuration Notes

**Service Account:** Must run as `.\azureuser` for certificate access

**Config Path Selection:** C# startup loads config in this precedence:
1. `--config <path>` command-line argument
2. `TALESTRAL_CONFIG_PATH` environment variable
3. default `Config/appsettings.json`

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
- Check which STT provider is active in `Config/appsettings.json` (`Stt.Provider`)
- For Deepgram: verify API key is valid by running `dotnet run --project DeepgramTest`
- For Deepgram: test connectivity: `Test-NetConnection api.deepgram.com -Port 443`
- For Azure Speech: check key and region in appsettings.json
- For Azure Speech: test connectivity: `Test-NetConnection eastus.api.cognitive.microsoft.com -Port 443`

**Service won't start (logon failure):**
- Password for `.\azureuser` is stale in NSSM
- Fix: `nssm set TeamsMediaBot ObjectName ".\azureuser" "CURRENT_PASSWORD"`
- Then: `Start-Service TeamsMediaBot`

**Deployment updates not applying:**
- Ensure service restarted after code changes
- Check service running: `Get-Service TeamsMediaBot`
- Verify correct working directory: `nssm get TeamsMediaBot AppDirectory`
