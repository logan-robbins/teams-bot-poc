# Teams Real-time Audio Transcription Bot (POC)

A proof-of-concept bot that joins Microsoft Teams meetings, receives real-time audio, transcribes using Azure Speech, and streams transcripts to a Python agent framework.

**Status:** ✅ All Azure resources provisioned and ready
**Last Updated:** 2026-01-29

---

## 🎯 What This Does

1. **Joins Teams meetings** via meeting join URL
2. **Receives real-time audio** using Microsoft Graph Communications app-hosted media
3. **Transcribes audio** using Azure Speech Service (continuous recognition)
4. **Streams transcripts** to Python endpoint for agent consumption
5. **Works on Mac + Windows VM** (hybrid development workflow)

---

## 📋 Prerequisites

### Mac Development Machine
- ✅ macOS (any version)
- ✅ Azure CLI (logged in as global admin)
- ✅ Python 3.9+
- ✅ Git
- ✅ Domain you control (for DNS)
- ✅ SSL certificate (wildcard for `*.botpoc.YOURDOMAIN.com`)

### Windows VM (Required - Cannot run on Mac)
- ✅ Windows 10/11 or Windows Server 2022
- ✅ Visual Studio 2022
- ✅ .NET 8.0 SDK
- ✅ ngrok
- ✅ Certificate installed in LocalMachine\My store

**Why Windows?** Per Microsoft docs, the `Microsoft.Graph.Communications.Calls.Media` library and real-time media platform only work on Windows.

---

## ✅ Azure Resources (Already Created)

| Resource | Status | Details |
|----------|--------|---------|
| **Resource Group** | ✅ Created | `rg-teams-media-bot-poc` (eastus) |
| **App Registration** | ✅ Created | App ID: `ff4b0902-5ae8-450b-bf45-7e2338292554` |
| **Graph Permissions** | ✅ Granted | `Calls.AccessMedia.All`, `Calls.JoinGroupCall.All` |
| **Azure Bot** | ✅ Created | `teams-media-bot-poc` |
| **Teams Channel** | ✅ Enabled | Ready for calling |
| **Speech Service** | ✅ Created | `speech-teams-bot-poc` (eastus) |

See `CONFIG.md` for all credentials and configuration values.

---

## 🚀 Quick Start

### Step 1: Mac Setup (Complete) ✅

All Azure resources are provisioned. Configuration values are in `CONFIG.md`.

### Step 2: DNS Setup (Required)

Create a CNAME record at your DNS provider:

```
0.botpoc.YOURDOMAIN.com → 0.tcp.ngrok.io
```

**Why?** Per Microsoft docs, app-hosted media requires a DNS name you control that maps to ngrok's fixed TCP hosts.

### Step 3: SSL Certificate (Required)

Obtain a wildcard certificate for `*.botpoc.YOURDOMAIN.com`:

**Options:**
- Let's Encrypt (free, requires DNS validation)
- Commercial CA (Sectigo, DigiCert, etc.)
- Corporate certificate

**Export as PFX** (you'll import this on Windows VM):
```bash
# On Mac (if using Let's Encrypt)
openssl pkcs12 -export -out bot-cert.pfx \
  -inkey privkey.pem \
  -in fullchain.pem \
  -passout pass:YourPassword
```

### Step 4: Windows VM Setup

**Option A: Local VM (Parallels/VMware)**
- Faster for development
- No ongoing costs
- Ngrok required

**Option B: Azure Windows VM**
- Always-on for demos
- No local performance impact
- Stable public IP (no ngrok for media)

See `SETUP-WINDOWS.md` for detailed instructions.

### Step 5: Transfer Code to Windows

**Option A: Git (Recommended)**
```bash
# On Mac
cd ~/research/teams/teams-bot-poc
git init
git remote add origin https://github.com/YOUR-REPO.git
git add .
git commit -m "Initial commit"
git push origin main

# On Windows
cd C:\dev
git clone https://github.com/YOUR-REPO.git teams-bot-poc
```

**Option B: Shared Folder (Parallels/VMware)**
- Enable folder sharing in VM settings
- Mac folder appears at `\\Mac\Home\...` in Windows

### Step 6: Configure on Windows

1. **Install certificate:**
   - Open `mmc.exe`
   - Add Certificates snap-in (Computer account, Local computer)
   - Import PFX to Personal → Certificates
   - Copy thumbprint

2. **Update `src/Config/appsettings.json`:**
   ```json
   {
     "MediaPlatformSettings": {
       "CertificateThumbprint": "PASTE_THUMBPRINT_HERE",
       "ServiceFqdn": "0.botpoc.YOURDOMAIN.com",
       "InstancePublicPort": 12345  // Update after ngrok starts
     },
     "Bot": {
       "NotificationUrl": "https://XXXXX.ngrok-free.app/api/calling"
     }
   }
   ```

3. **Update `scripts/ngrok.yml`:**
   ```yaml
   authtoken: YOUR_NGROK_AUTHTOKEN
   ```

### Step 7: Start Services

**On Mac (Terminal 1):**
```bash
cd python
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python transcript_sink.py
```

**On Windows (PowerShell 1):**
```powershell
cd C:\dev\teams-bot-poc\scripts
ngrok start --all --config ngrok.yml

# Note the output:
# - HTTPS: https://abc123.ngrok-free.app
# - TCP: tcp://0.tcp.ngrok.io:12345
```

**Update appsettings.json with ngrok values, then:**

**On Windows (Visual Studio):**
1. Open `src/TeamsMediaBot.csproj`
2. Press F5 to run
3. Wait for "Teams Media Bot POC starting..." log

### Step 8: Update Azure Bot Webhook

1. Go to https://portal.azure.com
2. Navigate to: Azure Bot → `teams-media-bot-poc` → Channels → Teams
3. Click **Calling** tab
4. Check **Enable calling**
5. Set **Webhook (for calling):** `https://abc123.ngrok-free.app/api/calling`
6. Save

### Step 9: Upload Teams App

1. Update `manifest/manifest.json`:
   - Replace `CHANGE_ME.ngrok-free.app` with your ngrok subdomain
   - Replace `0.botpoc.YOURDOMAIN.com` with your DNS

2. Create icons (192x192 and 32x32) - see `manifest/README.md`

3. Create ZIP:
   ```bash
   cd manifest
   zip teams-bot-poc.zip manifest.json color.png outline.png
   ```

4. Upload in Teams:
   - Teams → Apps → Upload a custom app
   - Select `teams-bot-poc.zip`
   - Click Add

### Step 10: Test!

1. **Create a test meeting** in Teams
2. **Copy the join URL**
3. **Tell bot to join:**

   ```bash
   curl -X POST https://abc123.ngrok-free.app/api/calling/join \
     -H "Content-Type: application/json" \
     -d '{"joinUrl":"PASTE_TEAMS_JOIN_URL","displayName":"Transcription Bot"}'
   ```

4. **Speak in the meeting**
5. **Watch transcripts appear** in your Python terminal!

---

## 📁 Project Structure

```
teams-bot-poc/
├── src/
│   ├── TeamsMediaBot.csproj          # C# project file
│   ├── Program.cs                     # ASP.NET Core startup
│   ├── Controllers/
│   │   └── CallingController.cs       # Webhook + join endpoint
│   ├── Services/
│   │   ├── TeamsCallingBotService.cs  # Core bot (join meetings, receive audio)
│   │   ├── AzureSpeechRealtimeTranscriber.cs  # Speech-to-text
│   │   └── PythonTranscriptPublisher.cs       # HTTP POST to Python
│   ├── Models/
│   │   ├── BotConfiguration.cs        # Config models
│   │   └── TranscriptEvent.cs         # Event DTO
│   └── Config/
│       └── appsettings.json           # Configuration
│
├── python/
│   ├── transcript_sink.py             # FastAPI receiver
│   └── requirements.txt               # Python dependencies
│
├── scripts/
│   ├── setup-azure-mac.sh            # Azure infrastructure setup (Mac)
│   ├── setup-windows.ps1             # Windows VM setup script
│   └── ngrok.yml                     # ngrok configuration
│
├── manifest/
│   ├── manifest.json                 # Teams app manifest
│   ├── color.png                     # 192x192 app icon
│   └── outline.png                   # 32x32 outline icon
│
├── docs/
│   ├── SETUP-WINDOWS.md             # Detailed Windows setup
│   └── mac-development-workflow.md   # Mac+Windows workflow
│
├── CONFIG.md                        # All credentials and config
└── README.md                        # This file
```

---

## 🔧 Configuration Reference

### Critical Configuration Files

1. **`src/Config/appsettings.json`** - Main bot configuration
2. **`scripts/ngrok.yml`** - ngrok tunnels
3. **`manifest/manifest.json`** - Teams app manifest
4. **`CONFIG.md`** - All Azure credentials

### What You Must Update

| File | Field | Value |
|------|-------|-------|
| `appsettings.json` | `CertificateThumbprint` | Your cert thumbprint |
| `appsettings.json` | `ServiceFqdn` | `0.botpoc.YOURDOMAIN.com` |
| `appsettings.json` | `InstancePublicPort` | Ngrok TCP port |
| `appsettings.json` | `NotificationUrl` | Ngrok HTTPS URL + `/api/calling` |
| `ngrok.yml` | `authtoken` | Your ngrok auth token |
| `manifest.json` | `validDomains[]` | Ngrok domain + media domain |

---

## 🐛 Troubleshooting

### Bot joins meeting but no audio

1. ✅ Confirm `ServiceFqdn` matches your DNS (NOT `0.tcp.ngrok.io`)
2. ✅ Confirm DNS CNAME exists and resolves
3. ✅ Confirm certificate subject/SAN covers `ServiceFqdn`
4. ✅ Confirm ngrok TCP tunnel is running
5. ✅ Confirm `InstancePublicPort` matches ngrok TCP port

### Speech recognition produces no text

1. ✅ Check logs for "Audio stats" (should see frames/sec)
2. ✅ Confirm Speech Service key is correct
3. ✅ Check Python endpoint is reachable from Windows VM
4. ✅ Look for "Canceled" events in logs (auth/region errors)

### Error 7504 / "Insufficient enterprise tenant permissions"

Your tenant is not provisioned for Cloud Communications calling APIs. Open a Microsoft support request to enable it.

### Can't upload custom app in Teams

Enable custom app upload in Teams admin center → App permission policy.

---

## 📚 Documentation

- **`CONFIG.md`** - All credentials, Azure resource details
- **`SETUP-WINDOWS.md`** - Complete Windows VM setup guide
- **`docs/mac-development-workflow.md`** - Mac + Windows development workflow
- **`docs/development-workflow.md`** - Git workflow, shared folders
- **`manifest/README.md`** - Teams app manifest guide
- **`teams-media-bot-poc-guide-validated-2026-updated.md`** - Complete reference guide (2000+ lines)

---

## 🎓 What This POC Demonstrates

✅ **Microsoft Graph Communications SDK** - Create/join calls  
✅ **Application-hosted media** - Receive raw audio frames  
✅ **Azure Speech SDK** - Real-time continuous recognition  
✅ **Hybrid Mac/Windows development** - Code on Mac, run on Windows  
✅ **Agent integration pattern** - Stream transcripts via HTTP/queue  

---

## ⚠️ Limitations (POC Scope)

❌ No scaling (single bot instance)  
❌ No video support (audio-only)  
❌ No recording compliance (`updateRecordingStatus`)  
❌ No production hardening  
❌ No Teams Store publishing  
❌ Local dev only (ngrok tunnels)  

---

## 📞 Need Help?

1. Check `teams-media-bot-poc-guide-validated-2026-updated.md` (comprehensive troubleshooting)
2. Check bot logs in Visual Studio console
3. Check Python logs for transcript events
4. Check Azure Portal → Bot → Channels → Test in Web Chat

---

## 🚀 Next Steps (Beyond POC)

- [ ] Deploy to Azure Windows VM (no ngrok)
- [ ] Add speaker diarization (identify who said what)
- [ ] Add sentiment analysis on transcripts
- [ ] Integrate with agent framework (LangChain, custom agent)
- [ ] Add video support (`supportsVideo: true`)
- [ ] Implement recording compliance
- [ ] Scale to multiple bot instances

---

**Built with:** Microsoft Graph Communications SDK, Azure Speech Service, ASP.NET Core, FastAPI  
**Architecture:** Application-hosted media (real-time audio access)  
**Validated:** 2026-01-29 against Microsoft Learn documentation  
