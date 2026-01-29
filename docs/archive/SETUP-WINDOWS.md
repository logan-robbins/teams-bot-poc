# Windows VM Setup Guide
**Teams Media Bot POC**

This guide walks through setting up the Windows environment needed to run the Teams media bot.

---

## Why Windows is Required

Per Microsoft documentation:
- The `Microsoft.Graph.Communications.Calls.Media` library **only works on Windows**
- Application-hosted media bots require Windows kernel features
- **Cannot run on macOS, Linux, Docker, or WSL**

Sources: Microsoft Learn docs S3, S4 (see main guide for references)

---

## Option 1: Local Windows VM (Parallels/VMware)

### Pros
- Fastest development iteration
- No ongoing costs (after VM software)
- Shared folders with Mac (instant code sync)

### Cons
- Requires Windows license
- Uses local machine resources (RAM, CPU)
- Ngrok required (URLs change on restart)

### Setup Steps

#### 1.1 Install VM Software

**Parallels Desktop** (Recommended for M-series Macs):
- Download: https://www.parallels.com
- Cost: ~$100/year
- Best ARM support for Apple Silicon

**VMware Fusion** (Alternative):
- Download: https://www.vmware.com/products/fusion.html
- Cost: ~$200 one-time
- Works on Intel and ARM

**UTM** (Free option):
- Download: https://mac.getutm.app
- Cost: Free (open source)
- ARM-native

#### 1.2 Install Windows

1. Download Windows 11 Pro ISO:
   - https://www.microsoft.com/software-download/windows11
   - Or use Windows Server 2022

2. Create new VM:
   - **RAM:** 8GB minimum (16GB recommended)
   - **CPU:** 4 cores minimum
   - **Disk:** 100GB

3. Install Windows:
   - Follow setup wizard
   - Skip Microsoft account (use local account for VM)

#### 1.3 Enable Shared Folders

**Parallels:**
1. VM → Settings → Options → Sharing
2. Check "Share Mac folders with Windows"
3. Select your project folder: `/Users/YOUR_USERNAME/research/teams/teams-bot-poc`
4. In Windows, access via: `\\Mac\Home\research\teams\teams-bot-poc`

**VMware:**
1. VM → Settings → Sharing
2. Enable shared folders
3. Add project folder
4. In Windows, access via: `\\vmware-host\Shared Folders\`

---

## Option 2: Azure Windows VM

### Pros
- Always-on (no need to keep Mac running)
- Stable public IP (no ngrok for media endpoint)
- No local resource usage

### Cons
- Ongoing Azure costs (~$140/month for recommended size)
- Network latency for RDP
- Must upload code to VM

### Setup Steps

#### 2.1 Create Azure VM (From Mac)

```bash
# Create VM
az vm create \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-dev \
  --image Win2022Datacenter \
  --size Standard_D4s_v3 \
  --admin-username azureuser \
  --admin-password 'SuperSecure123!' \
  --public-ip-sku Standard \
  --location eastus

# Open RDP port
az vm open-port \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-dev \
  --port 3389 \
  --priority 1000

# Open bot ports
az vm open-port \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-dev \
  --port 443 \
  --priority 1001

az vm open-port \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-dev \
  --port 8445 \
  --priority 1002

# Get public IP
az vm show \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-dev \
  --show-details \
  --query publicIps -o tsv
```

#### 2.2 Connect from Mac

1. **Download Microsoft Remote Desktop:**
   - Mac App Store: Search "Microsoft Remote Desktop"
   - Free app from Microsoft

2. **Add Connection:**
   - Click `+` → Add PC
   - PC name: `[Public IP from above]`
   - User account: `azureuser`
   - Password: `SuperSecure123!`
   - Save

3. **Connect:**
   - Double-click the connection
   - Accept certificate warning (first time)

---

## Windows Software Installation

### Required Software

Run these commands in PowerShell (Administrator):

#### 1. Install Chocolatey (Package Manager)

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
```

#### 2. Install Software via Chocolatey

```powershell
# Git
choco install git -y

# .NET SDK
choco install dotnet-sdk -y

# Visual Studio 2022 Community (includes MSBuild)
choco install visualstudio2022community -y

# Visual Studio workloads
choco install visualstudio2022-workload-netweb -y

# ngrok
choco install ngrok -y
```

#### 3. Manual Installs (if Chocolatey fails)

**Visual Studio 2022:**
1. Download: https://visualstudio.microsoft.com/downloads/
2. Run installer
3. Select workload: **ASP.NET and web development**
4. Install

**.NET 8.0 SDK:**
1. Download: https://dotnet.microsoft.com/download/dotnet/8.0
2. Run installer

**ngrok:**
1. Download: https://ngrok.com/download
2. Extract to `C:\ngrok`
3. Add to PATH: System Properties → Environment Variables → Path → Add `C:\ngrok`

---

## Certificate Installation

### 1. Transfer Certificate from Mac to Windows

**If using local VM with shared folders:**
```powershell
# In Windows, copy from Mac shared folder
Copy-Item "\\Mac\Home\path\to\bot-cert.pfx" -Destination "C:\Certs\bot-cert.pfx"
```

**If using Azure VM:**
```bash
# On Mac, use SCP
scp bot-cert.pfx azureuser@[VM_IP]:/C:/Certs/bot-cert.pfx
```

### 2. Install Certificate

```powershell
# Open Certificate Manager
mmc.exe
```

In MMC:
1. File → Add/Remove Snap-in
2. Select **Certificates** → Add
3. Select **Computer account** → Next → Local computer → Finish → OK
4. Expand: **Certificates (Local Computer)** → **Personal** → **Certificates**
5. Right-click **Certificates** → All Tasks → Import
6. Browse to `C:\Certs\bot-cert.pfx`
7. Enter password (if set)
8. **Certificate Store:** Personal
9. Finish

### 3. Get Certificate Thumbprint

```powershell
# List certificates and get thumbprint
Get-ChildItem Cert:\LocalMachine\My | 
  Where-Object {$_.Subject -like "*.botpoc.*"} |
  Select-Object Subject, Thumbprint, NotAfter |
  Format-List
```

Copy the thumbprint (example: `A1B2C3D4E5F6789012345678901234567890ABCD`)

---

## Clone/Transfer Code

### Option A: Git Clone (Recommended)

```powershell
# Create dev directory
New-Item -ItemType Directory -Path "C:\dev" -Force

# Clone repository
cd C:\dev
git clone https://github.com/YOUR-USERNAME/teams-bot-poc.git

cd teams-bot-poc
```

### Option B: Copy from Mac (Shared Folder)

```powershell
# Copy entire project
Copy-Item -Recurse "\\Mac\Home\research\teams\teams-bot-poc" -Destination "C:\dev\teams-bot-poc"
```

---

## Configure appsettings.json

Open `C:\dev\teams-bot-poc\src\Config\appsettings.json` in Notepad or Visual Studio.

Update these values:

```json
{
  "Bot": {
    "NotificationUrl": "https://YOUR-NGROK-SUBDOMAIN.ngrok-free.app/api/calling"
  },
  "MediaPlatformSettings": {
    "CertificateThumbprint": "PASTE_THUMBPRINT_FROM_ABOVE",
    "InstancePublicPort": 12345,  // Will update after ngrok starts
    "ServiceFqdn": "0.botpoc.YOURDOMAIN.com"
  }
}
```

---

## Configure ngrok

### 1. Get ngrok Auth Token

1. Sign up at https://ngrok.com
2. Go to https://dashboard.ngrok.com/get-started/your-authtoken
3. Copy your authtoken

### 2. Update ngrok.yml

Open `C:\dev\teams-bot-poc\scripts\ngrok.yml`:

```yaml
version: "2"
authtoken: YOUR_NGROK_AUTHTOKEN_HERE  # Paste token here

tunnels:
  signaling:
    proto: http
    addr: 9441
    
  media:
    proto: tcp
    addr: 8445
```

### 3. Start ngrok

```powershell
cd C:\dev\teams-bot-poc\scripts
ngrok start --all --config ngrok.yml
```

**Important:** Note these values from ngrok output:

```
Forwarding  https://abc123.ngrok-free.app -> http://localhost:9441   ← HTTPS URL
Forwarding  tcp://0.tcp.ngrok.io:12345 -> localhost:8445             ← TCP PORT
```

### 4. Update appsettings.json with ngrok values

```json
{
  "Bot": {
    "NotificationUrl": "https://abc123.ngrok-free.app/api/calling"
  },
  "MediaPlatformSettings": {
    "InstancePublicPort": 12345  // TCP port from ngrok
  }
}
```

---

## Build and Run

### Option A: Visual Studio (Recommended)

1. Open `C:\dev\teams-bot-poc\TeamsMediaBot.sln`
2. Wait for NuGet packages to restore
3. Build → Build Solution (Ctrl+Shift+B)
4. Debug → Start Debugging (F5)

**Expected output:**
```
Teams Media Bot POC starting on http://0.0.0.0:9441
Notification URL configured: https://abc123.ngrok-free.app/api/calling
Media endpoint: 0.botpoc.YOURDOMAIN.com:12345
```

### Option B: Command Line

```powershell
cd C:\dev\teams-bot-poc\src
dotnet restore
dotnet build --configuration Release
dotnet run --configuration Release
```

---

## Test the Bot

### 1. Start Python Receiver (On Mac)

```bash
cd ~/research/teams/teams-bot-poc/python
source .venv/bin/activate
python transcript_sink.py
```

### 2. Join a Meeting

**Create a test meeting in Teams and copy the join URL.**

Then send join request:

```powershell
# From Windows PowerShell
$body = @{
    joinUrl = "https://teams.microsoft.com/l/meetup-join/..."
    displayName = "Transcription Bot"
} | ConvertTo-Json

Invoke-RestMethod `
    -Uri "https://abc123.ngrok-free.app/api/calling/join" `
    -Method Post `
    -ContentType "application/json" `
    -Body $body
```

### 3. Verify

**In Visual Studio console, look for:**
```
Call created: [CALL_ID]
Call state changed: Establishing
Call state changed: Established
Audio media receive handler configured
Speech recognition session started
```

**In Python terminal, look for:**
```
[FINAL] Hello everyone
[FINAL] This is a test of the transcription bot
```

---

## Troubleshooting

### Build Errors: "Could not load file Microsoft.Graph.Communications..."

**Solution:**
```powershell
cd C:\dev\teams-bot-poc\src
dotnet clean
Remove-Item -Recurse -Force bin,obj
dotnet restore --force
dotnet build
```

### ngrok: "Account limit exceeded"

**Solution:** Upgrade to ngrok paid plan or use a different tunnel service.

Free tier limits:
- 1 agent online
- 40 connections/minute
- 4 tunnels/ngrok agent

### Bot joins but no media

**Checklist:**
1. ✅ Certificate installed in LocalMachine\My
2. ✅ Thumbprint matches in appsettings.json
3. ✅ ServiceFqdn is your DNS name (NOT 0.tcp.ngrok.io)
4. ✅ DNS CNAME exists and resolves
5. ✅ InstancePublicPort matches ngrok TCP port
6. ✅ ngrok TCP tunnel is running

### Speech recognition errors

**Check Speech Service key:**
```powershell
# Test Speech Service
curl "https://eastus.api.cognitive.microsoft.com/sts/v1.0/issueToken" `
    -H "Ocp-Apim-Subscription-Key: YOUR_SPEECH_KEY" `
    -X POST
```

Should return a JWT token. If it fails, check the key in appsettings.json.

### Python endpoint not reachable

**If using local VM:**
```bash
# On Mac: Find your IP
ifconfig en0 | grep "inet "

# Update appsettings.json:
"TranscriptSink": {
  "PythonEndpoint": "http://192.168.1.XXX:8765/transcript"
}

# Make sure Python is listening on all interfaces:
uvicorn transcript_sink:app --host 0.0.0.0 --port 8765
```

---

## Daily Development Workflow

### Start of Day

1. **Start Python receiver** (Mac):
   ```bash
   cd ~/research/teams/teams-bot-poc/python
   source .venv/bin/activate
   python transcript_sink.py
   ```

2. **Start ngrok** (Windows):
   ```powershell
   cd C:\dev\teams-bot-poc\scripts
   ngrok start --all --config ngrok.yml
   ```

3. **Update appsettings.json** if ngrok URL changed

4. **Update Azure Bot webhook** in portal if ngrok URL changed

5. **Run bot** (Visual Studio F5)

### Making Code Changes

**On Mac:**
```bash
# Edit code in VSCode/Cursor
code ~/research/teams/teams-bot-poc

# Commit and push
git add .
git commit -m "Update audio handler"
git push origin main
```

**On Windows:**
```powershell
# Pull changes
cd C:\dev\teams-bot-poc
git pull origin main

# Rebuild in Visual Studio (Ctrl+Shift+B)
# Restart (Ctrl+Shift+F5)
```

---

## Performance Tips

### Local VM

- **Allocate more RAM** (16GB+ for smooth experience)
- **Use SSD** for VM disk
- **Close unused apps** on Mac while VM is running
- **Use shared folders** (faster than Git for quick iterations)

### Azure VM

- **Use D4s_v3 or larger** (D2s_v3 will be slow)
- **Use Standard SSD** (not HDD)
- **Stop VM when not in use** (save costs)
- **Use proximity placement** (same region as other Azure resources)

---

## Next Steps

✅ Windows environment ready  
✅ Bot builds and runs  
✅ Connected to Teams  

**Now:**
1. Test joining meetings
2. Verify transcripts flow to Python
3. Integrate with your agent framework
4. Deploy to stable Azure VM (optional)

See main `README.md` for full usage guide!
