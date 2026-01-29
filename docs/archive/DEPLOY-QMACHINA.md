# Teams Media Bot - Production Deployment Guide (qmachina.com)

**Updated:** 2026-01-29  
**Architecture:** Azure VM + qmachina.com (no ngrok)

---

## 🎯 What Changed

**Before:** ngrok tunnels (URLs change on restart, free tier limits)  
**After:** Production deployment on qmachina.com (stable, always-on)

**Benefits:**
- ✅ No ngrok costs or limitations
- ✅ Stable URLs (never change)
- ✅ Production-ready architecture
- ✅ Professional setup
- ✅ Fully automated deployment

---

## 📋 Prerequisites

### 1. Azure Resources (Already Created ✅)
- Resource Group: `rg-teams-media-bot-poc`
- App Registration: `ff4b0902-5ae8-450b-bf45-7e2338292554`
- Azure Bot: `teams-media-bot-poc`
- Speech Service: `speech-teams-bot-poc`

### 2. DNS Access (Required)
- Access to qmachina.com DNS settings
- Ability to create A records

### 3. SSL Certificates (Required)
Choose ONE:
- **Option A:** Existing wildcard `*.qmachina.com` certificate
- **Option B:** Let's Encrypt (free, automated)
- **Option C:** Purchase specific certs for `teamsbot` and `media` subdomains

---

## 🚀 Deployment Steps (15 minutes)

### Step 1: Deploy Azure VM (3 minutes)

```bash
cd ~/research/teams/teams-bot-poc/scripts
chmod +x deploy-azure-vm.sh
./deploy-azure-vm.sh
```

**What this does:**
- Creates Windows Server 2022 VM (D4s_v3)
- Assigns static public IP
- Opens required ports (443, 8445, 3389)
- Installs all software (Git, .NET, etc.)
- Clones and builds the bot
- Creates Windows Service (auto-start)
- Starts the bot

**Output:**
```
VM Public IP: X.X.X.X
Admin Username: azureuser
Admin Password: SecureTeamsBot2026!
```

**Save this information!**

### Step 2: Configure DNS (2 minutes)

Go to your DNS provider for qmachina.com and create:

**Record 1:**
```
Type: A
Name: teamsbot
Value: [VM Public IP from Step 1]
TTL: 300
```

**Record 2:**
```
Type: A
Name: media
Value: [VM Public IP from Step 1]
TTL: 300
```

**Verify DNS (wait 5-15 minutes for propagation):**
```bash
nslookup teamsbot.qmachina.com
nslookup media.qmachina.com
```

### Step 3: SSL Certificates (5-30 minutes)

#### Option A: Use Existing Wildcard Certificate (Fastest)

If you have `*.qmachina.com` certificate:

1. **Export as PFX** (if not already):
   ```bash
   # On Mac
   openssl pkcs12 -export -out qmachina-wildcard.pfx \
     -inkey private.key \
     -in certificate.crt \
     -certfile ca-bundle.crt \
     -passout pass:YourPassword
   ```

2. **Upload to VM:**
   ```bash
   # Using SCP (if you enabled SSH) or use RDP
   # For now, use RDP - see Step 4
   ```

#### Option B: Let's Encrypt (Free, requires DNS validation)

On the VM (via RDP):
```powershell
# Install win-acme
choco install win-acme -y

# Get certificates (interactive)
wacs.exe
# Choose: N (New certificate)
# Choose: 2 (Manual input)
# Enter: teamsbot.qmachina.com
# Follow DNS-01 validation steps
```

Repeat for `media.qmachina.com`.

#### Option C: Purchase Certificates

1. Buy certificates from CA (Sectigo, DigiCert, etc.)
2. Complete domain validation
3. Download as PFX format
4. Transfer to VM

### Step 4: Install Certificates on VM (5 minutes)

**Connect to VM:**
1. Open Microsoft Remote Desktop (Mac)
2. Add PC: `[VM Public IP]`
3. Username: `azureuser`
4. Password: `SecureTeamsBot2026!`

**On VM:**
```powershell
# Import certificate
$certPath = "C:\certs\qmachina-wildcard.pfx"  # Or your cert path
$certPassword = ConvertTo-SecureString -String "YourPassword" -Force -AsPlainText

Import-PfxCertificate `
  -FilePath $certPath `
  -CertStoreLocation Cert:\LocalMachine\My `
  -Password $certPassword

# Get thumbprint
Get-ChildItem Cert:\LocalMachine\My | 
  Where-Object {$_.Subject -like "*qmachina*"} |
  Select-Object Thumbprint, Subject, NotAfter
```

**Copy the thumbprint** (40-character hex string).

### Step 5: Update Configuration (2 minutes)

**On VM, edit:**
```powershell
notepad C:\teams-bot-poc\src\Config\appsettings.json
```

**Update:**
```json
{
  "MediaPlatformSettings": {
    "CertificateThumbprint": "PASTE_THUMBPRINT_HERE"
  }
}
```

**Restart service:**
```powershell
Restart-Service TeamsMediaBot
```

**Verify it's running:**
```powershell
Get-Service TeamsMediaBot
Get-Content C:\teams-bot-poc\logs\service-output.log -Wait -Tail 20
```

### Step 6: Update Azure Bot Webhook (1 minute)

1. Go to https://portal.azure.com
2. Navigate to: Resource Groups → `rg-teams-media-bot-poc` → `teams-media-bot-poc`
3. Go to: Channels → Microsoft Teams → Calling tab
4. Set **Webhook (for calling):** `https://teamsbot.qmachina.com/api/calling`
5. Click **Apply**

### Step 7: Test Health Check (1 minute)

```bash
curl https://teamsbot.qmachina.com/api/calling/health
```

**Expected response:**
```json
{
  "Status": "Healthy",
  "Timestamp": "2026-01-29T...",
  "Service": "Teams Media Bot POC"
}
```

---

## ✅ Deployment Complete!

Your bot is now:
- ✅ Running on production VM
- ✅ Accessible via stable qmachina.com URLs
- ✅ Auto-starts on VM reboot
- ✅ Logging to files
- ✅ Ready to join Teams meetings

---

## 🧪 Testing

### 1. Start Python Receiver (On Your Mac)

```bash
cd ~/research/teams/teams-bot-poc/python
source .venv/bin/activate
python transcript_sink.py
```

**Note:** The VM will try to POST to `http://127.0.0.1:8765/transcript`. For production, you'd want to:
- Run Python receiver on the VM, OR
- Update `TranscriptSink.PythonEndpoint` to point to a cloud endpoint

### 2. Create Test Meeting

1. In Teams, create a meeting
2. Join the meeting
3. Copy the join URL

### 3. Tell Bot to Join

```bash
curl -X POST https://teamsbot.qmachina.com/api/calling/join \
  -H "Content-Type: application/json" \
  -d '{"joinUrl":"PASTE_JOIN_URL","displayName":"Transcription Bot"}'
```

### 4. Verify

**Check VM logs:**
```powershell
# On VM
Get-Content C:\teams-bot-poc\logs\service-output.log -Wait -Tail 50
```

**Expected:**
```
Call created: [CALL_ID]
Call state changed: Established
Audio media receive handler configured
Speech recognition session started
Audio stats: 50 frames, 32000 bytes (1.0s)
```

**In Teams:**
- Bot should appear as participant
- Speak and verify transcripts are generated

---

## 🔧 Operations

### View Logs
```powershell
# Real-time
Get-Content C:\teams-bot-poc\logs\service-output.log -Wait

# Last 50 lines
Get-Content C:\teams-bot-poc\logs\service-output.log -Tail 50
```

### Restart Bot
```powershell
Restart-Service TeamsMediaBot
```

### Stop Bot
```powershell
Stop-Service TeamsMediaBot
```

### Update Code
```powershell
cd C:\teams-bot-poc
git pull origin main
cd src
dotnet build --configuration Release
Restart-Service TeamsMediaBot
```

### Check Service Status
```powershell
Get-Service TeamsMediaBot
```

---

## 💰 Monthly Costs

| Service | Cost |
|---------|------|
| VM (D4s_v3) | ~$140 |
| Speech Service | ~$1-5 |
| Azure Bot | $0 (free tier) |
| DNS (qmachina.com) | $0 (existing) |
| SSL (Let's Encrypt) | $0 (free) |
| **Total** | **~$145/month** |

**To stop costs:**
```bash
az vm deallocate --name vm-teams-bot-prod --resource-group rg-teams-media-bot-poc
```

**To delete everything:**
```bash
az vm delete --name vm-teams-bot-prod --resource-group rg-teams-media-bot-poc --yes
```

---

## 🐛 Troubleshooting

### Bot service won't start

**Check logs:**
```powershell
Get-Content C:\teams-bot-poc\logs\service-error.log
```

**Common issues:**
- Certificate thumbprint incorrect
- Port 443 already in use
- .NET not installed correctly

### DNS not resolving

```bash
# Check DNS propagation
dig teamsbot.qmachina.com
nslookup teamsbot.qmachina.com

# Wait 15 minutes for full propagation
```

### SSL certificate errors

**Verify certificate is installed:**
```powershell
Get-ChildItem Cert:\LocalMachine\My | Format-List
```

**Verify subject matches domain:**
- Certificate subject should be `*.qmachina.com` OR `teamsbot.qmachina.com`
- Thumbprint in appsettings.json should match exactly

### Bot joins but no audio

1. **Check Media DNS:**
   ```bash
   nslookup media.qmachina.com
   # Should resolve to VM public IP
   ```

2. **Check firewall:**
   ```powershell
   # On VM
   Get-NetFirewallRule -DisplayName "Teams Bot Media"
   ```

3. **Check logs for media connection:**
   ```powershell
   Get-Content C:\teams-bot-poc\logs\service-output.log | Select-String "media"
   ```

---

## 📚 Additional Resources

- **Full architecture:** `ARCHITECTURE-PRODUCTION.md`
- **Original guide:** `teams-media-bot-poc-guide-validated-2026-updated.md`
- **Configuration reference:** `CONFIG.md`
- **Quick commands:** `QUICK-REFERENCE.md`

---

## 🎉 Success!

Your Teams Media Bot is now:
- ✅ Production-deployed on qmachina.com
- ✅ No ngrok dependencies
- ✅ Stable, reliable URLs
- ✅ Auto-starting Windows Service
- ✅ Ready for real meetings

**Next:** Integrate with your agent framework via the transcript endpoint! 🚀
