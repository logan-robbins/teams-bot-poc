# Teams Media Bot POC - Setup Checklist

Use this checklist to track your progress through the setup process.

---

## ✅ Phase 1: Mac Setup (COMPLETE)

- [x] Azure CLI logged in as global admin
- [x] Resource group created (`rg-teams-media-bot-poc`)
- [x] Entra app registration created
- [x] Client secret generated
- [x] Graph permissions added and admin consent granted
- [x] Azure Bot resource created
- [x] Teams channel enabled
- [x] Speech Service created
- [x] All credentials saved in `CONFIG.md`
- [x] Complete C# codebase generated
- [x] Python receiver created
- [x] Documentation complete

**Time:** ✅ Complete (~10 minutes)

---

## ⏳ Phase 2: DNS & Certificate (YOUR TASK)

### DNS Setup

- [ ] Choose domain for bot (e.g., `botpoc.example.com`)
- [ ] Create CNAME record:
  ```
  Name: 0.botpoc
  Type: CNAME
  Value: 0.tcp.ngrok.io
  TTL: 300
  ```
- [ ] Verify DNS resolves:
  ```bash
  nslookup 0.botpoc.YOURDOMAIN.com
  # Should show it aliases to 0.tcp.ngrok.io
  ```

**Time estimate:** 5-10 minutes (depending on DNS propagation)

### SSL Certificate

Choose ONE option:

**Option A: Let's Encrypt (Free)**
- [ ] Install certbot: `brew install certbot`
- [ ] Get wildcard cert:
  ```bash
  sudo certbot certonly --manual --preferred-challenges dns \
    -d "*.botpoc.YOURDOMAIN.com"
  ```
- [ ] Complete DNS TXT record validation
- [ ] Export as PFX:
  ```bash
  openssl pkcs12 -export -out ~/Desktop/bot-cert.pfx \
    -inkey /etc/letsencrypt/live/botpoc.YOURDOMAIN.com/privkey.pem \
    -in /etc/letsencrypt/live/botpoc.YOURDOMAIN.com/fullchain.pem
  ```

**Option B: Commercial CA**
- [ ] Purchase wildcard certificate for `*.botpoc.YOURDOMAIN.com`
- [ ] Complete domain validation
- [ ] Download certificate as PFX format
- [ ] Save to `~/Desktop/bot-cert.pfx`

**Option C: Corporate Certificate**
- [ ] Request wildcard cert from IT department
- [ ] Receive PFX file
- [ ] Save to `~/Desktop/bot-cert.pfx`

**Time estimate:** 15-60 minutes (depending on method)

---

## ⏳ Phase 3: Windows VM Setup (YOUR TASK)

### Choose Your Approach

**Option A: Local VM (Parallels/VMware)**
- [ ] Install Parallels Desktop or VMware Fusion
- [ ] Download Windows 11 Pro ISO
- [ ] Create VM (8GB+ RAM, 4+ cores, 100GB disk)
- [ ] Install Windows
- [ ] Configure network (Shared/Bridged mode)
- [ ] Enable shared folders

**Option B: Azure Windows VM**
- [ ] Create VM using Azure Portal or CLI
- [ ] Open required ports (3389, 443, 8445)
- [ ] Get public IP address
- [ ] Download Microsoft Remote Desktop (Mac App Store)
- [ ] Connect via RDP

**Time estimate:** 30-60 minutes

### Install Required Software on Windows

- [ ] Open PowerShell as Administrator
- [ ] Install Chocolatey package manager
- [ ] Install Git: `choco install git -y`
- [ ] Install .NET 8.0 SDK: `choco install dotnet-sdk -y`
- [ ] Install Visual Studio 2022 Community: `choco install visualstudio2022community -y`
- [ ] Install ASP.NET workload: `choco install visualstudio2022-workload-netweb -y`
- [ ] Install ngrok: `choco install ngrok -y`
- [ ] Restart Windows

**Time estimate:** 20-30 minutes

### Verify Installation

- [ ] Run verification script:
  ```powershell
  cd C:\dev\teams-bot-poc\scripts
  .\setup-windows.ps1
  ```
- [ ] Confirm all required software is installed

---

## ⏳ Phase 4: Code Transfer (YOUR TASK)

### Option A: Git (Recommended)

**On Mac:**
- [ ] Create GitHub repository (or GitLab/Bitbucket)
- [ ] Initialize git:
  ```bash
  cd ~/research/teams/teams-bot-poc
  git init
  git remote add origin https://github.com/YOUR-USERNAME/teams-bot-poc.git
  ```
- [ ] Commit code:
  ```bash
  git add .
  git commit -m "Initial Teams bot implementation"
  git push origin main
  ```

**On Windows:**
- [ ] Clone repository:
  ```powershell
  cd C:\dev
  git clone https://github.com/YOUR-USERNAME/teams-bot-poc.git
  cd teams-bot-poc
  ```

**Time estimate:** 5 minutes

### Option B: Shared Folder (Parallels/VMware only)

- [ ] Enable shared folders in Parallels/VMware settings
- [ ] Share Mac folder: `/Users/YOUR-USERNAME/research/teams/teams-bot-poc`
- [ ] Access in Windows at: `\\Mac\Home\research\teams\teams-bot-poc`
- [ ] Copy to C:\dev if desired (or work directly from shared folder)

**Time estimate:** 2 minutes

---

## ⏳ Phase 5: Windows Configuration (YOUR TASK)

### Certificate Installation

- [ ] Transfer `bot-cert.pfx` from Mac to Windows
- [ ] Open `mmc.exe`
- [ ] Add Certificates snap-in (Computer account, Local computer)
- [ ] Import PFX to Personal → Certificates
- [ ] Get thumbprint:
  ```powershell
  Get-ChildItem Cert:\LocalMachine\My | 
    Where-Object {$_.Subject -like "*.botpoc.*"} |
    Select-Object Thumbprint, Subject, NotAfter
  ```
- [ ] Copy thumbprint value (40-character hex string)

### Update appsettings.json (First Pass)

Open `C:\dev\teams-bot-poc\src\Config\appsettings.json`:

- [ ] Update `MediaPlatformSettings.CertificateThumbprint` with actual thumbprint
- [ ] Update `MediaPlatformSettings.ServiceFqdn` to `0.botpoc.YOURDOMAIN.com`
- [ ] Leave `NotificationUrl` and `InstancePublicPort` for now (will update after ngrok)

### Configure ngrok

- [ ] Sign up at https://ngrok.com
- [ ] Get authtoken from https://dashboard.ngrok.com/get-started/your-authtoken
- [ ] Update `C:\dev\teams-bot-poc\scripts\ngrok.yml`:
  ```yaml
  authtoken: YOUR_ACTUAL_NGROK_AUTHTOKEN
  ```

**Time estimate:** 10 minutes

---

## ⏳ Phase 6: Start Services (YOUR TASK)

### Start Python Receiver (Mac Terminal)

- [ ] Open Terminal on Mac
- [ ] Run:
  ```bash
  cd ~/research/teams/teams-bot-poc/python
  python -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  python transcript_sink.py
  ```
- [ ] Verify output shows: "Starting Teams Transcript Sink on http://127.0.0.1:8765"
- [ ] Leave running

### Start ngrok (Windows PowerShell)

- [ ] Open PowerShell on Windows
- [ ] Run:
  ```powershell
  cd C:\dev\teams-bot-poc\scripts
  ngrok start --all --config ngrok.yml
  ```
- [ ] Note the two forwarding URLs:
  ```
  HTTPS: https://abc123.ngrok-free.app → http://localhost:9441
  TCP: tcp://0.tcp.ngrok.io:12345 → localhost:8445
  ```
- [ ] Copy these values (you'll need them next)
- [ ] Leave running

### Update appsettings.json (Second Pass)

In `C:\dev\teams-bot-poc\src\Config\appsettings.json`:

- [ ] Update `Bot.NotificationUrl` to: `https://YOUR-NGROK-SUBDOMAIN.ngrok-free.app/api/calling`
- [ ] Update `MediaPlatformSettings.InstancePublicPort` to ngrok TCP port (e.g., `12345`)
- [ ] If using local VM, update `TranscriptSink.PythonEndpoint` to Mac's IP:
  ```json
  "TranscriptSink": {
    "PythonEndpoint": "http://192.168.1.XXX:8765/transcript"
  }
  ```
- [ ] Save file

**Time estimate:** 5 minutes

---

## ⏳ Phase 7: Azure Bot Configuration (YOUR TASK)

### Update Calling Webhook

- [ ] Open Azure Portal: https://portal.azure.com
- [ ] Navigate to: Resource Groups → `rg-teams-media-bot-poc` → `teams-media-bot-poc`
- [ ] Go to: Channels → Microsoft Teams
- [ ] Click the **Calling** tab
- [ ] Check **Enable calling**
- [ ] Set **Webhook (for calling)** to: `https://YOUR-NGROK.ngrok-free.app/api/calling`
- [ ] Click **Apply** / **Save**

**Time estimate:** 2 minutes

---

## ⏳ Phase 8: Build and Run Bot (YOUR TASK)

### Build in Visual Studio

- [ ] Open `C:\dev\teams-bot-poc\TeamsMediaBot.sln` in Visual Studio
- [ ] Wait for NuGet packages to restore (watch status bar)
- [ ] Build → Build Solution (Ctrl+Shift+B)
- [ ] Verify no errors in Output window

### Run the Bot

- [ ] Press F5 (Start Debugging)
- [ ] Verify console output shows:
  ```
  Teams Media Bot POC starting on http://0.0.0.0:9441
  Notification URL configured: https://YOUR-NGROK.ngrok-free.app/api/calling
  Media endpoint: 0.botpoc.YOURDOMAIN.com:12345
  Teams Calling Bot Service initialized successfully
  ```
- [ ] Leave running

**Time estimate:** 5 minutes

---

## ⏳ Phase 9: Create Teams App Package (YOUR TASK)

### Create Icon Files

You need two PNG images:
- `color.png` (192x192px)
- `outline.png` (32x32px)

**Quick creation:**
```bash
# Option 1: Use ImageMagick (on Mac)
cd ~/research/teams/teams-bot-poc/manifest
convert -size 192x192 xc:'#0078D4' -fill white -pointsize 120 -gravity center -annotate +0+0 'TB' color.png
convert -size 32x32 xc:white -fill '#0078D4' -pointsize 24 -gravity center -annotate +0+0 'TB' outline.png

# Option 2: Create online at https://www.canva.com (192x192 and 32x32 images)
```

- [ ] Create or download `color.png` (192x192)
- [ ] Create or download `outline.png` (32x32)
- [ ] Save to `manifest/` folder

### Update Manifest

Edit `manifest/manifest.json`:

- [ ] Replace `CHANGE_ME.ngrok-free.app` with your actual ngrok subdomain
- [ ] Replace `0.botpoc.YOURDOMAIN.com` with your actual DNS name
- [ ] Save file

### Create ZIP Package

```bash
cd ~/research/teams/teams-bot-poc/manifest
zip teams-bot-poc.zip manifest.json color.png outline.png
```

- [ ] Create ZIP file
- [ ] Verify it contains all 3 files at root level

### Upload to Teams

- [ ] Open Microsoft Teams
- [ ] Go to: Apps
- [ ] Click: **Upload a custom app** (at bottom)
- [ ] Select `teams-bot-poc.zip`
- [ ] Click **Add**
- [ ] Verify app appears in your Teams apps list

**Time estimate:** 10 minutes

---

## ⏳ Phase 10: Test! (YOUR TASK)

### Create Test Meeting

- [ ] In Teams, create a meeting:
  - Click **Calendar** → **New meeting**
  - Set title: "Bot Test"
  - Set time: Now or upcoming
  - Click **Save**
- [ ] Open the meeting
- [ ] Click **Join** (you must be in the meeting)
- [ ] Copy the meeting join URL:
  - Click `...` → **Meeting details**
  - Copy the "Join the meeting" link

### Join Bot to Meeting

**Method 1: Using curl (Mac Terminal):**
```bash
curl -X POST https://YOUR-NGROK.ngrok-free.app/api/calling/join \
  -H "Content-Type: application/json" \
  -d '{"joinUrl":"PASTE_TEAMS_JOIN_URL_HERE","displayName":"Transcription Bot"}'
```

**Method 2: Using PowerShell (Windows):**
```powershell
$body = @{
    joinUrl = "PASTE_TEAMS_JOIN_URL_HERE"
    displayName = "Transcription Bot"
} | ConvertTo-Json

Invoke-RestMethod `
    -Uri "https://YOUR-NGROK.ngrok-free.app/api/calling/join" `
    -Method Post `
    -ContentType "application/json" `
    -Body $body
```

- [ ] Send join request
- [ ] Verify response includes `CallId`

### Verify Bot Joined

In Visual Studio console, look for:
- [ ] `Call created: [CALL_ID]`
- [ ] `Call state changed: Establishing`
- [ ] `Call state changed: Established`
- [ ] `Audio media receive handler configured`
- [ ] `Speech recognition session started`
- [ ] `Audio stats: X frames, Y bytes...` (repeating)

In Teams meeting:
- [ ] Verify "Transcription Bot" appears as a participant

### Test Transcription

- [ ] Speak clearly in the meeting: "Hello, this is a test"
- [ ] Watch Python terminal for transcripts:
  ```
  [FINAL] hello this is a test
  ```
- [ ] Verify transcripts appear in near real-time

**Time estimate:** 5 minutes

---

## ✅ Success Criteria

You've successfully completed the POC when:

- [x] All Azure resources are created
- [ ] DNS resolves correctly
- [ ] Certificate is installed on Windows
- [ ] Bot builds without errors
- [ ] Bot starts and shows "initialized successfully"
- [ ] Bot can join Teams meetings
- [ ] Audio frames are being received (~50 fps)
- [ ] Speech recognition produces text
- [ ] Transcripts appear in Python terminal
- [ ] No errors in bot logs
- [ ] No errors in Python logs

---

## 📊 Progress Summary

**Phase 1 (Mac Setup):** ✅ COMPLETE  
**Phase 2 (DNS & Certificate):** ⏳ PENDING  
**Phase 3 (Windows VM):** ⏳ PENDING  
**Phase 4 (Code Transfer):** ⏳ PENDING  
**Phase 5 (Configuration):** ⏳ PENDING  
**Phase 6 (Start Services):** ⏳ PENDING  
**Phase 7 (Azure Bot Config):** ⏳ PENDING  
**Phase 8 (Build & Run):** ⏳ PENDING  
**Phase 9 (Teams App):** ⏳ PENDING  
**Phase 10 (Test):** ⏳ PENDING  

**Estimated remaining time:** 2-3 hours (first time)

---

## 🆘 Troubleshooting

If something doesn't work:

1. **Check `DEPLOYMENT-SUMMARY.md`** → "Common Issues & Quick Fixes"
2. **Check bot logs** in Visual Studio console
3. **Check Python logs** in Mac terminal
4. **Check `SETUP-WINDOWS.md`** for detailed Windows troubleshooting
5. **Check `teams-media-bot-poc-guide-validated-2026-updated.md`** Part F (Troubleshooting)

---

## 🎉 You're Ready!

**Current Status:**
- ✅ All code is ready
- ✅ All Azure resources are provisioned
- ✅ All documentation is complete

**Next Step:**
Start with Phase 2 (DNS & Certificate) and work through the checklist!

**Good luck! 🚀**
