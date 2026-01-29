# Teams Media Bot - Production Architecture (qmachina.com)

**Updated:** 2026-01-29  
**Change:** Using real domain instead of ngrok for stable, production-ready deployment

---

## 🏗️ Architecture Overview

### Before (ngrok):
```
Teams → ngrok HTTPS (changes on restart) → Local bot
Teams → ngrok TCP (changes on restart) → Local media port
```

### After (qmachina.com):
```
Teams → teamsbot.qmachina.com (stable) → Azure VM :443
Teams → media.qmachina.com (stable) → Azure VM :8445
```

**Benefits:**
- ✅ No ngrok (no cost, no limitations, no URL changes)
- ✅ Stable endpoints (no configuration updates needed)
- ✅ Production-ready architecture
- ✅ Can fully automate deployment
- ✅ Always-on (no local dev machine needed)

---

## 📊 Infrastructure Components

### 1. Azure Windows VM
```
Name: vm-teams-bot-prod
Size: Standard_D4s_v3 (4 vCPU, 16GB RAM)
OS: Windows Server 2022 Datacenter
Location: eastus
Public IP: Static (Standard SKU)
Ports: 443 (HTTPS), 8445 (Media), 3389 (RDP for admin)
```

### 2. DNS Records (qmachina.com)
```
teamsbot.qmachina.com → A → [VM Public IP]
media.qmachina.com → A → [VM Public IP]
```

### 3. SSL Certificates
```
Certificate 1: teamsbot.qmachina.com (HTTPS signaling)
Certificate 2: *.media.qmachina.com or media.qmachina.com (Media endpoint)

Options:
- Let's Encrypt (free, automated renewal)
- Azure App Service Certificate
- Existing qmachina.com wildcard cert
```

### 4. Network Security Group
```
Priority 1000: Allow TCP 443 (HTTPS) from Internet
Priority 1001: Allow TCP 8445 (Media) from Internet
Priority 1002: Allow TCP 3389 (RDP) from your IP only
```

---

## 🔧 Updated Configuration

### appsettings.json (Final Values)
```json
{
  "Bot": {
    "TenantId": "2843abed-8970-461e-a260-a59dc1398dbf",
    "AppId": "ff4b0902-5ae8-450b-bf45-7e2338292554",
    "AppSecret": "aAu8Q~WY.C2fIk~Ezr0Q4Ch~j9YP6nNto14y4bnK",
    "NotificationUrl": "https://teamsbot.qmachina.com/api/calling",
    "LocalHttpListenUrl": "http://0.0.0.0:443",
    "LocalHttpListenPort": 443
  },
  "MediaPlatformSettings": {
    "ApplicationId": "ff4b0902-5ae8-450b-bf45-7e2338292554",
    "CertificateThumbprint": "CERT_THUMBPRINT_AFTER_INSTALL",
    "InstanceInternalPort": 8445,
    "InstancePublicPort": 8445,
    "ServiceFqdn": "media.qmachina.com",
    "InstancePublicIPAddress": "0.0.0.0"
  },
  "Speech": {
    "Key": "4PMljn6sqJzjGUoNu2WXt64Aqmrl6PN1Ev9cbx9tGad1S5wmUn2bJQQJ99CAACYeBjFXJ3w3AAAYACOGOsek",
    "Region": "eastus",
    "RecognitionLanguage": "en-US"
  },
  "TranscriptSink": {
    "PythonEndpoint": "http://127.0.0.1:8765/transcript"
  }
}
```

### Azure Bot Webhook (One-time setup)
```
https://teamsbot.qmachina.com/api/calling
```

### Teams App Manifest (validDomains)
```json
"validDomains": [
  "teamsbot.qmachina.com",
  "media.qmachina.com",
  "qmachina.com"
]
```

---

## 🚀 Deployment Steps (Fully Automated)

### Step 1: Create Azure VM with Static IP
```bash
# Create VM with static public IP
az vm create \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --image Win2022Datacenter \
  --size Standard_D4s_v3 \
  --admin-username azureuser \
  --admin-password 'SecurePassword123!' \
  --public-ip-sku Standard \
  --public-ip-address-allocation static \
  --location eastus

# Get public IP
PUBLIC_IP=$(az vm show -d \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --query publicIps -o tsv)

echo "VM Public IP: $PUBLIC_IP"
```

### Step 2: Configure NSG (Firewall Rules)
```bash
# Allow HTTPS (443)
az vm open-port \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --port 443 \
  --priority 1000

# Allow Media (8445)
az vm open-port \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --port 8445 \
  --priority 1001

# Allow RDP (3389) - restrict to your IP
az vm open-port \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --port 3389 \
  --priority 1002
```

### Step 3: Configure DNS at qmachina.com
```
DNS Provider: (Your DNS provider for qmachina.com)

Create A Records:
Name: teamsbot
Type: A
Value: [VM Public IP]
TTL: 300

Name: media
Type: A
Value: [VM Public IP]
TTL: 300
```

### Step 4: Get SSL Certificates

**Option A: Let's Encrypt (Free, Automated)**
```powershell
# On VM (via certbot or win-acme)
# Requires DNS-01 or HTTP-01 challenge
# Can be fully automated
```

**Option B: Azure App Service Certificate**
```bash
# Purchase wildcard cert for *.qmachina.com
# Auto-renews, exports as PFX
```

**Option C: Existing Wildcard Certificate**
```
If you already have *.qmachina.com certificate:
- Export as PFX
- Upload to VM
- Import to LocalMachine\My
```

### Step 5: Automated VM Setup (Remote Script)
```bash
# Upload and run setup script
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts @deploy-production.ps1
```

---

## 📦 Deployment Automation

### Full deployment script covers:
1. ✅ Software installation (Git, .NET SDK, etc.)
2. ✅ Code deployment (Git clone)
3. ✅ Certificate import
4. ✅ Configuration update
5. ✅ Windows service creation (auto-start on boot)
6. ✅ Health check endpoint
7. ✅ Log rotation
8. ✅ Monitoring setup

---

## 🔒 Security Improvements

### With qmachina.com vs ngrok:
- ✅ **SSL/TLS**: Real certificate (not ngrok's)
- ✅ **Firewall**: Azure NSG rules
- ✅ **Rate limiting**: Can implement at VM level
- ✅ **IP restrictions**: Can restrict sources
- ✅ **No third-party tunnels**: Direct connection
- ✅ **Audit logs**: Full control

---

## 💰 Cost Comparison

### Old (ngrok):
```
ngrok paid plan: ~$8-25/month
+ Local dev machine running 24/7: Electricity + wear
Total: ~$10-50/month + local resources
```

### New (Azure VM):
```
VM (D4s_v3): ~$140/month
Speech Service: ~$1-5/month
Azure Bot: $0 (free tier)
DNS: $0 (existing qmachina.com)
SSL: $0 (Let's Encrypt) or included in wildcard

Total: ~$145/month
```

**Benefits justify cost:**
- Always-on production environment
- No local machine dependency
- Stable, professional setup
- Can scale if needed

---

## 📈 Scalability Path

### Current (POC):
```
1 VM, 1 bot instance
Handles: 1 meeting at a time
```

### Future (Production):
```
Multiple VMs behind Azure Load Balancer
Each handles: Multiple concurrent meetings
Auto-scaling based on load
```

---

## 🔄 CI/CD Pipeline (Future)

### Automated deployment:
```
GitHub → GitHub Actions → Build → Test → Deploy to Azure VM
```

### Rollback capability:
```
Keep previous versions
One-command rollback if needed
```

---

## 🎯 Production Readiness Checklist

- [x] Real domain (qmachina.com)
- [x] Static public IP
- [x] Proper SSL certificates
- [ ] Automated deployment script
- [ ] Windows Service (auto-start)
- [ ] Log rotation
- [ ] Monitoring/alerting
- [ ] Backup/restore procedures
- [ ] Documentation
- [ ] Load testing
- [ ] Disaster recovery plan

---

## 🚦 Traffic Flow

### Signaling (HTTPS):
```
Teams Cloud
  ↓ HTTPS
https://teamsbot.qmachina.com/api/calling
  ↓ Port 443
Azure VM (Public IP)
  ↓ Local
ASP.NET Core Bot (Port 443)
```

### Media (TCP):
```
Teams Cloud
  ↓ TCP
media.qmachina.com:8445
  ↓ Port 8445
Azure VM (Public IP)
  ↓ Local
Media Platform (Port 8445)
```

### Transcripts:
```
Bot → HTTP POST → Python Receiver (Port 8765)
```

---

## 📝 Operations Guide

### Start Bot (Automatic via Windows Service)
```powershell
Start-Service TeamsMediaBot
```

### Stop Bot
```powershell
Stop-Service TeamsMediaBot
```

### View Logs
```powershell
Get-Content C:\teams-bot-poc\logs\bot.log -Wait -Tail 100
```

### Check Status
```powershell
# Health check
Invoke-RestMethod https://teamsbot.qmachina.com/api/calling/health

# Process status
Get-Service TeamsMediaBot
```

### Update Code
```powershell
cd C:\teams-bot-poc
git pull origin main
dotnet build --configuration Release
Restart-Service TeamsMediaBot
```

---

**Next Steps:** Ready to deploy to production with qmachina.com! 🚀
