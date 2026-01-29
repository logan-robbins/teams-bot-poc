# Quick Reference Guide

Essential commands and configurations for daily use.

---

## 🔑 Azure Resources

```bash
Resource Group: rg-teams-media-bot-poc
Location: eastus

App ID: ff4b0902-5ae8-450b-bf45-7e2338292554
Tenant ID: 2843abed-8970-461e-a260-a59dc1398dbf
Speech Key: 4PMljn6sqJzjGUoNu2WXt64Aqmrl6PN1Ev9cbx9tGad1S5wmUn2bJQQJ99CAACYeBjFXJ3w3AAAYACOGOsek
```

See `CONFIG.md` for full details.

---

## 🚀 Daily Startup Sequence

### 1. Start Python Receiver (Mac)
```bash
cd ~/research/teams/teams-bot-poc/python
source .venv/bin/activate
python transcript_sink.py
```

### 2. Start ngrok (Windows)
```powershell
cd C:\dev\teams-bot-poc\scripts
ngrok start --all --config ngrok.yml
```

### 3. Update appsettings.json (if ngrok URL changed)
Edit: `C:\dev\teams-bot-poc\src\Config\appsettings.json`
- Update `Bot.NotificationUrl`
- Update `MediaPlatformSettings.InstancePublicPort`

### 4. Update Azure Bot Webhook (if ngrok URL changed)
Azure Portal → Bot → Channels → Teams → Calling → Webhook

### 5. Run Bot (Visual Studio)
Press F5

---

## 📞 Join Meeting

### Get Meeting URL
Teams → Meeting → `...` → Meeting details → Copy join link

### Send Join Request

**Using curl (Mac):**
```bash
curl -X POST https://YOUR-NGROK.ngrok-free.app/api/calling/join \
  -H "Content-Type: application/json" \
  -d '{"joinUrl":"TEAMS_JOIN_URL","displayName":"Transcription Bot"}'
```

**Using PowerShell (Windows):**
```powershell
$body = @{joinUrl="TEAMS_JOIN_URL"; displayName="Transcription Bot"} | ConvertTo-Json
Invoke-RestMethod -Uri "https://YOUR-NGROK.ngrok-free.app/api/calling/join" -Method Post -ContentType "application/json" -Body $body
```

---

## 🔍 Health Checks

### Check Bot Health
```bash
curl https://YOUR-NGROK.ngrok-free.app/api/calling/health
```

### Check Python Receiver
```bash
curl http://localhost:8765/health
```

### Check Python Stats
```bash
curl http://localhost:8765/stats
```

### Check Certificate
```powershell
Get-ChildItem Cert:\LocalMachine\My | Where-Object {$_.Subject -like "*.botpoc.*"} | Select-Object Thumbprint, NotAfter
```

---

## 📂 Key File Locations

### Mac
```
Project root: ~/research/teams/teams-bot-poc
Python receiver: ~/research/teams/teams-bot-poc/python/transcript_sink.py
Config: ~/research/teams/teams-bot-poc/CONFIG.md
```

### Windows
```
Project root: C:\dev\teams-bot-poc
Solution: C:\dev\teams-bot-poc\TeamsMediaBot.sln
Config: C:\dev\teams-bot-poc\src\Config\appsettings.json
ngrok config: C:\dev\teams-bot-poc\scripts\ngrok.yml
```

---

## 🛠️ Common Tasks

### Rebuild Project
```powershell
cd C:\dev\teams-bot-poc\src
dotnet clean
dotnet build --configuration Release
```

### Pull Latest Code (Git)
```powershell
cd C:\dev\teams-bot-poc
git pull origin main
```

### View Logs
- **Bot:** Visual Studio console output
- **Python:** Terminal where `transcript_sink.py` is running
- **File logs:** `C:\dev\teams-bot-poc\logs\`

### Restart Everything
1. Stop Visual Studio (Shift+F5)
2. Stop ngrok (Ctrl+C)
3. Stop Python (Ctrl+C on Mac)
4. Start in reverse order (Python → ngrok → Bot)

---

## 🐛 Quick Troubleshooting

### Bot won't start
```powershell
# Check .NET version
dotnet --version  # Should be 8.0+

# Check for port conflicts
netstat -ano | findstr :9441
netstat -ano | findstr :8445
```

### No audio frames
Check Visual Studio console for:
```
Audio stats: X frames, Y bytes
```

If missing:
1. Verify cert thumbprint in appsettings.json
2. Verify ServiceFqdn is YOUR DNS (not ngrok's)
3. Verify DNS CNAME exists
4. Verify ngrok TCP tunnel is running

### No transcripts in Python
```bash
# On Mac: Get your IP
ipconfig getifaddr en0

# Update appsettings.json:
"PythonEndpoint": "http://192.168.1.XXX:8765/transcript"

# Verify Python is listening:
netstat -an | grep 8765
```

---

## 📝 Configuration Values You'll Update Often

### appsettings.json
```json
{
  "Bot": {
    "NotificationUrl": "https://XXXX.ngrok-free.app/api/calling"  // Changes with ngrok
  },
  "MediaPlatformSettings": {
    "InstancePublicPort": 12345  // Changes with ngrok
  }
}
```

### Azure Bot Webhook
Portal → Bot → Channels → Teams → Calling  
Webhook: `https://XXXX.ngrok-free.app/api/calling`

---

## 📊 Expected Performance

**Audio:**
- Frame rate: ~50 frames/sec (20ms each)
- Frame size: ~640 bytes (16kHz 16-bit mono PCM)
- Bandwidth: ~256 kbps

**Transcription:**
- Partial results: <500ms latency
- Final results: <2s latency
- Accuracy: Depends on audio quality and Speech Service

**Resources:**
- Bot memory: 200-300MB
- Python memory: 50MB
- CPU: <10% (on modern hardware)

---

## 🔗 Useful URLs

**Azure Portal:**
- Resource Group: https://portal.azure.com/#@/resource/subscriptions/70464868-52ea-435d-93a6-8002e83f0b89/resourceGroups/rg-teams-media-bot-poc
- Bot: https://portal.azure.com/#@/resource/subscriptions/70464868-52ea-435d-93a6-8002e83f0b89/resourceGroups/rg-teams-media-bot-poc/providers/Microsoft.BotService/botServices/teams-media-bot-poc

**Documentation:**
- Microsoft Graph Communications: https://learn.microsoft.com/en-us/graph/cloud-communications-get-started
- Azure Speech SDK: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/

**Tools:**
- ngrok Dashboard: https://dashboard.ngrok.com
- Teams App Studio: In Teams → Apps → "App Studio" or "Developer Portal"

---

## 💡 Pro Tips

1. **Save ngrok URLs:** Keep a text file with current ngrok URLs for easy reference
2. **Use shared folders:** Faster than Git for rapid iteration (Parallels/VMware)
3. **Monitor stats:** Watch Python `/stats` endpoint to track transcript volume
4. **Check logs first:** 90% of issues are visible in Visual Studio console
5. **Test audio quality:** Use good microphone in quiet environment for best results
6. **Stop when done:** Stop bot and ngrok to avoid using tunnels when not needed

---

## 📞 Support Resources

1. **CHECKLIST.md** - Step-by-step setup tasks
2. **DEPLOYMENT-SUMMARY.md** - What's done, what's next
3. **SETUP-WINDOWS.md** - Complete Windows guide (40+ pages)
4. **README.md** - Project overview
5. **teams-media-bot-poc-guide-validated-2026-updated.md** - Complete reference (2000+ lines)

---

**Remember:** This is a POC. Focus on getting it working, not perfecting it! 🚀
