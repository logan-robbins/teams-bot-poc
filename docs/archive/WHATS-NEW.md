# What's New - Production Architecture (qmachina.com)

**Updated:** 2026-01-29  
**Major Change:** Switched from ngrok to production deployment on qmachina.com

---

## 🎯 Summary of Changes

### Old Approach (ngrok)
```
Developer runs bot locally on Mac/Windows
↓
ngrok creates temporary tunnels
↓
Teams connects via ngrok URLs
↓
URLs change every restart
↓
Must update Azure Bot webhook each time
```

**Problems:**
- URLs change on every ngrok restart
- Free tier limitations (40 connections/min, 4 tunnels)
- Local machine must always be running
- Not production-ready
- Additional cost for paid ngrok plan

### New Approach (qmachina.com)
```
Bot runs on Azure Windows VM
↓
Stable qmachina.com DNS names
↓
Teams connects directly to VM
↓
URLs never change
↓
Production-ready, always-on
```

**Benefits:**
- ✅ Stable URLs (never change)
- ✅ No ngrok costs or limitations
- ✅ Production-ready architecture
- ✅ Always-on (no local machine needed)
- ✅ Professional setup
- ✅ Can fully automate
- ✅ Easier to maintain

---

## 📊 Architecture Comparison

### URLs

| Component | Old (ngrok) | New (qmachina.com) |
|-----------|-------------|---------------------|
| **Signaling** | `https://abc123.ngrok-free.app/api/calling` | `https://teamsbot.qmachina.com/api/calling` |
| **Media** | `tcp://0.tcp.ngrok.io:12345` via CNAME | `media.qmachina.com:8445` |
| **Stability** | Changes on restart ❌ | Never changes ✅ |

### Infrastructure

| Component | Old (ngrok) | New (qmachina.com) |
|-----------|-------------|---------------------|
| **Hosting** | Local dev machine | Azure Windows VM |
| **Availability** | Only when dev machine on | 24/7 always-on |
| **DNS** | CNAME to ngrok hosts | A records to VM IP |
| **SSL** | Your cert + ngrok's cert | Your certificates only |
| **Firewall** | ngrok's network | Azure NSG (full control) |

---

## 🔧 What Changed in the Code

### Configuration Files

**appsettings.json:**
```json
// OLD
"NotificationUrl": "https://XXXXX.ngrok-free.app/api/calling"
"LocalHttpListenPort": 9441
"ServiceFqdn": "0.botpoc.YOURDOMAIN.com"
"InstancePublicPort": 12345  // ngrok TCP port

// NEW
"NotificationUrl": "https://teamsbot.qmachina.com/api/calling"
"LocalHttpListenPort": 443
"ServiceFqdn": "media.qmachina.com"
"InstancePublicPort": 8445  // Direct VM port
```

**manifest.json:**
```json
// OLD
"validDomains": [
  "XXXXX.ngrok-free.app",
  "0.botpoc.YOURDOMAIN.com"
]

// NEW
"validDomains": [
  "teamsbot.qmachina.com",
  "media.qmachina.com",
  "qmachina.com"
]
```

### Deployment Scripts

**New files:**
- `scripts/deploy-azure-vm.sh` - Creates VM and deploys bot
- `scripts/deploy-production.ps1` - Runs on VM to set up everything
- `ARCHITECTURE-PRODUCTION.md` - Production architecture guide
- `DEPLOY-QMACHINA.md` - Step-by-step deployment guide

**Removed:**
- `scripts/ngrok.yml` - Not needed anymore!

---

## 💰 Cost Comparison

### Old (ngrok approach)
```
ngrok paid plan: $8-25/month (if you hit free limits)
+ Local dev machine running 24/7: Electricity + wear

Total: ~$10-50/month + must keep computer running
```

### New (qmachina.com)
```
Azure VM (D4s_v3): $140/month
Speech Service: $1-5/month
Azure Bot: $0 (free tier)
SSL (Let's Encrypt): $0 (free)

Total: ~$145/month
```

**Why it's worth it:**
- ✅ Professional production setup
- ✅ Always available (no local machine dependency)
- ✅ Stable URLs
- ✅ Can scale if needed
- ✅ No manual restarts needed

---

## 🚀 Deployment Process

### Old Process (ngrok)
1. Start bot locally
2. Start ngrok
3. Get ngrok URLs
4. Update appsettings.json
5. Restart bot
6. Update Azure Bot webhook
7. Test
8. **Repeat steps 2-7 every time ngrok restarts**

**Time per deployment:** 10-15 minutes  
**Frequency:** Every restart/reboot

### New Process (qmachina.com)
1. Run deployment script once: `./deploy-azure-vm.sh`
2. Create DNS records (one-time)
3. Install SSL certificates (one-time)
4. Done!

**Time for initial deployment:** 15 minutes  
**Frequency:** Once (URLs never change)

**Updates:**
```bash
# Just push code and restart service
git push origin main
# On VM:
git pull && dotnet build && Restart-Service TeamsMediaBot
```

---

## 📝 DNS Setup

### What You Need to Do

Go to your DNS provider for qmachina.com and create these A records:

```
Record 1:
  Type: A
  Name: teamsbot
  Value: [VM Public IP from deployment]
  TTL: 300

Record 2:
  Type: A
  Name: media
  Value: [VM Public IP from deployment]
  TTL: 300
```

**That's it!** These never change.

---

## 🔒 SSL Certificates

### Options

**Option 1: Use Existing Wildcard** (Fastest)
- If you have `*.qmachina.com` certificate
- Works for both `teamsbot` and `media` subdomains
- Just import to VM

**Option 2: Let's Encrypt** (Free)
- Get free certificates
- Auto-renewal available
- Requires DNS validation

**Option 3: Purchase** (Most control)
- Buy specific certificates
- Longer validity
- Professional CA

---

## 🎯 Migration Path

### If You Already Have the Bot Running with ngrok

**Quick migration:**
1. Run `./deploy-azure-vm.sh` (creates VM and deploys)
2. Create DNS records
3. Install certificates
4. Update Azure Bot webhook to new URL
5. Done!

**Old local setup still works** - You can run both simultaneously for testing.

---

## ✅ Benefits Summary

| Feature | Old (ngrok) | New (qmachina.com) |
|---------|-------------|---------------------|
| **URL Stability** | Changes on restart | Never changes ✅ |
| **Availability** | Only when dev machine on | 24/7 ✅ |
| **Professional** | Development setup | Production-ready ✅ |
| **Maintenance** | Manual restarts | Auto-starts ✅ |
| **Automation** | Limited | Fully automated ✅ |
| **Scalability** | Single machine | Can scale ✅ |
| **Monitoring** | Manual logs | Windows Service logs ✅ |
| **Cost** | ~$10-50/month + local machine | ~$145/month ✅ |

---

## 🚦 Next Steps

### For New Deployments
1. Read `DEPLOY-QMACHINA.md`
2. Run `./scripts/deploy-azure-vm.sh`
3. Follow the setup steps
4. Test!

### For Existing Installations
1. Keep your current setup running (if you want)
2. Deploy the new production VM alongside it
3. Test the production deployment
4. Switch over when ready
5. Shut down local/ngrok setup

---

## 📚 Updated Documentation

**New/Updated files:**
- `ARCHITECTURE-PRODUCTION.md` - Complete production architecture
- `DEPLOY-QMACHINA.md` - Step-by-step deployment guide
- `scripts/deploy-azure-vm.sh` - One-command deployment
- `scripts/deploy-production.ps1` - VM setup automation
- `WHATS-NEW.md` - This file
- `appsettings.json` - Updated with qmachina.com URLs
- `manifest.json` - Updated with qmachina.com domains
- `CONFIG.md` - Updated configuration guide

**Files you can ignore now:**
- `scripts/ngrok.yml` - Not needed with qmachina.com
- References to ngrok in other docs

---

## 🎉 Ready to Deploy!

**The bot is now production-ready with:**
- ✅ Stable qmachina.com URLs
- ✅ Fully automated deployment
- ✅ Always-on Windows Service
- ✅ Professional architecture
- ✅ No ngrok dependencies

**Deploy now:**
```bash
cd ~/research/teams/teams-bot-poc/scripts
./deploy-azure-vm.sh
```

**Total deployment time: ~15 minutes** 🚀
