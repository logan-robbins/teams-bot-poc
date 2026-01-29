# Teams Media Bot POC - Start Here! 🚀

**Last Updated:** 2026-01-29  
**Status:** ✅ Ready to deploy to production

---

## ✅ What's Already Complete

### Azure Infrastructure (100% Complete)
- ✅ Resource Group created
- ✅ App Registration with permissions
- ✅ Azure Bot configured
- ✅ Teams Channel enabled
- ✅ Speech Service provisioned
- ✅ All credentials generated

### Codebase (100% Complete)
- ✅ Complete C# bot implementation (~1,200 lines)
- ✅ Python transcript receiver (~150 lines)
- ✅ Production-ready configuration
- ✅ Deployment automation scripts
- ✅ Comprehensive documentation

### Architecture (Production-Ready)
- ✅ Using qmachina.com domain (no ngrok!)
- ✅ Stable URLs that never change
- ✅ Fully automated deployment
- ✅ Windows Service (auto-start)
- ✅ Professional setup

---

## 🎯 What You Need to Do (15 minutes)

### Step 1: Deploy Azure VM (3 minutes)

```bash
cd ~/research/teams/teams-bot-poc/scripts
./deploy-azure-vm.sh
```

**What this does:**
- Creates Windows Server VM
- Installs all software automatically
- Builds and starts the bot
- Creates Windows Service
- Returns VM public IP address

**Output you'll get:**
```
VM Public IP: X.X.X.X
Admin Username: azureuser
Admin Password: SecureTeamsBot2026!
```

### Step 2: Configure DNS (2 minutes)

Go to your DNS provider for qmachina.com:

**Create these A records:**
```
teamsbot.qmachina.com → [VM Public IP]
media.qmachina.com → [VM Public IP]
```

**Verify:**
```bash
nslookup teamsbot.qmachina.com
nslookup media.qmachina.com
```

### Step 3: SSL Certificates (5 minutes)

**Do you have a wildcard certificate for `*.qmachina.com`?**

- **YES:** Skip to Step 4 (just import it to the VM)
- **NO:** Get one now (options below)

**Options:**
1. **Let's Encrypt** (free, 5 minutes): Use win-acme on VM
2. **Purchase** (paid, 15-30 minutes): Buy from CA
3. **Use existing** (instant): If you have corp/wildcard cert

### Step 4: Install Certificates (3 minutes)

**RDP to VM:**
```
IP: [VM Public IP from Step 1]
Username: azureuser
Password: SecureTeamsBot2026!
```

**On VM:**
```powershell
# Import certificate
Import-PfxCertificate -FilePath C:\path\to\cert.pfx `
  -CertStoreLocation Cert:\LocalMachine\My `
  -Password (ConvertTo-SecureString -String "YourPassword" -AsPlainText -Force)

# Get thumbprint
Get-ChildItem Cert:\LocalMachine\My | 
  Where-Object {$_.Subject -like "*qmachina*"} |
  Select Thumbprint
```

### Step 5: Update Config & Restart (2 minutes)

**On VM, edit config:**
```powershell
notepad C:\teams-bot-poc\src\Config\appsettings.json
```

**Update this line:**
```json
"CertificateThumbprint": "PASTE_YOUR_THUMBPRINT_HERE"
```

**Restart:**
```powershell
Restart-Service TeamsMediaBot
```

### Step 6: Update Azure Bot Webhook (1 minute)

1. Go to: https://portal.azure.com
2. Navigate to: `rg-teams-media-bot-poc` → `teams-media-bot-poc`
3. Go to: Channels → Teams → Calling
4. Set webhook: `https://teamsbot.qmachina.com/api/calling`
5. Save

---

## 🧪 Test It! (5 minutes)

### 1. Health Check

```bash
curl https://teamsbot.qmachina.com/api/calling/health
```

Expected:
```json
{"Status":"Healthy","Timestamp":"...","Service":"Teams Media Bot POC"}
```

### 2. Join a Meeting

**Create Teams meeting, then:**
```bash
curl -X POST https://teamsbot.qmachina.com/api/calling/join \
  -H "Content-Type: application/json" \
  -d '{"joinUrl":"YOUR_TEAMS_JOIN_URL","displayName":"Transcription Bot"}'
```

### 3. Verify

**On VM, check logs:**
```powershell
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
- Bot appears as participant ✅
- Speak and verify transcripts are generated ✅

---

## 📚 Documentation Guide

### Quick Start
- **This file** - Overview and deployment steps
- `DEPLOY-QMACHINA.md` - Detailed deployment guide
- `WHATS-NEW.md` - What changed from ngrok to qmachina.com

### Reference
- `CONFIG.md` - All credentials and configuration
- `ARCHITECTURE-PRODUCTION.md` - Complete architecture
- `QUICK-REFERENCE.md` - Common commands

### Troubleshooting
- `SETUP-WINDOWS.md` - Windows VM detailed guide
- `teams-media-bot-poc-guide-validated-2026-updated.md` - Complete 2000+ line reference

### Code
- `src/` - Complete C# bot implementation
- `python/` - Transcript receiver
- `manifest/` - Teams app package

---

## 💰 Cost

**Monthly:**
- VM (D4s_v3): ~$140
- Speech Service: ~$1-5
- **Total: ~$145/month**

**To stop costs:**
```bash
az vm deallocate --name vm-teams-bot-prod --resource-group rg-teams-media-bot-poc
```

---

## 🎯 Architecture Overview

```
Teams Meeting
    ↓ HTTPS (signaling)
https://teamsbot.qmachina.com/api/calling
    ↓
Azure VM (Windows Server)
    ↓
Teams Media Bot (C# / ASP.NET Core)
    ↓ Real-time audio frames (50/sec)
Azure Speech Service
    ↓ Transcript events
Python Receiver (FastAPI)
    ↓
Your Agent Framework
```

**Key Points:**
- ✅ No ngrok - direct connection
- ✅ Stable URLs - never change
- ✅ Production-ready - Windows Service
- ✅ Always-on - auto-starts on reboot

---

## 🚨 Prerequisites You Need

### For Deployment
- [x] Azure subscription (already logged in)
- [ ] DNS access for qmachina.com
- [ ] SSL certificate (wildcard or specific)

### For Testing
- [ ] Microsoft Teams account
- [ ] Ability to create meetings
- [ ] Microphone to speak in meeting

---

## ⚡ Quick Deploy (One Command)

**If you have DNS access and certificates ready:**

```bash
# Deploy everything
cd ~/research/teams/teams-bot-poc/scripts
./deploy-azure-vm.sh

# Follow the on-screen instructions for:
# 1. DNS setup
# 2. Certificate installation
# 3. Configuration update

# Total time: ~15 minutes
```

---

## 🎉 Success Criteria

You'll know it's working when:

✅ Health check returns 200 OK  
✅ Bot joins Teams meeting successfully  
✅ Bot appears as participant in Teams  
✅ Audio frames are received (~50/sec)  
✅ Speech recognition produces transcripts  
✅ Transcripts appear in Python receiver  

---

## 🆘 Need Help?

**Issue:** DNS not resolving
- **Solution:** Wait 15 minutes for propagation, verify A records

**Issue:** Certificate errors
- **Solution:** Check thumbprint matches, verify cert subject includes qmachina.com

**Issue:** Bot won't start
- **Solution:** Check logs: `Get-Content C:\teams-bot-poc\logs\service-error.log`

**Issue:** No audio frames
- **Solution:** Verify media DNS resolves, check firewall port 8445

**Full troubleshooting:** See `DEPLOY-QMACHINA.md` troubleshooting section

---

## 🚀 Ready to Deploy?

**Run this now:**

```bash
cd ~/research/teams/teams-bot-poc/scripts
./deploy-azure-vm.sh
```

**Then follow the output instructions for DNS and SSL setup.**

**Total deployment time: ~15 minutes to live bot!** 🎉

---

## 📞 What Happens Next

After deployment:
1. Your bot is live on `teamsbot.qmachina.com`
2. It's running as a Windows Service (auto-starts)
3. It's logging to files on the VM
4. You can join it to any Teams meeting
5. It will transcribe audio in real-time
6. Transcripts flow to Python for agent processing

**This is production-ready!** You can now:
- Integrate with your agent framework
- Scale to multiple VMs if needed
- Add monitoring/alerting
- Implement additional features

**The foundation is solid. Build on it!** 🚀
