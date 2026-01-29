# Teams Media Bot POC - Configuration Reference

## Azure Resources Created

**Tenant ID:** `2843abed-8970-461e-a260-a59dc1398dbf`
**Subscription:** `70464868-52ea-435d-93a6-8002e83f0b89`
**Resource Group:** `rg-teams-media-bot-poc`

### Entra App Registration
- **App (Client) ID:** `ff4b0902-5ae8-450b-bf45-7e2338292554`
- **Client Secret:** `aAu8Q~WY.C2fIk~Ezr0Q4Ch~j9YP6nNto14y4bnK`
- **Permissions Granted:**
  - `Calls.AccessMedia.All` (Application)
  - `Calls.JoinGroupCall.All` (Application)
  - Admin consent: ✅ Granted

### Azure Bot
- **Name:** `teams-media-bot-poc`
- **Bot ID:** `ff4b0902-5ae8-450b-bf45-7e2338292554`
- **Endpoint:** `https://placeholder.ngrok-free.app/api/calling` (update after ngrok starts)
- **Status:** ✅ Created
- **Teams Channel:** ✅ Enabled

### Azure Speech Service
- **Name:** `speech-teams-bot-poc`
- **Region:** `eastus`
- **SKU:** `S0`
- **Key:** `4PMljn6sqJzjGUoNu2WXt64Aqmrl6PN1Ev9cbx9tGad1S5wmUn2bJQQJ99CAACYeBjFXJ3w3AAAYACOGOsek`
- **Endpoint:** `https://eastus.api.cognitive.microsoft.com/`
- **Status:** ✅ Created

---

## Production Configuration (qmachina.com)

### 1. DNS Configuration (Required)
```
Create A records at your DNS provider for qmachina.com:

teamsbot.qmachina.com → A → [Azure VM Public IP]
media.qmachina.com → A → [Azure VM Public IP]
```

### 2. SSL Certificates (Required)
```
Obtain certificates for:
- teamsbot.qmachina.com (HTTPS signaling)
- media.qmachina.com (Media endpoint)

OR use existing wildcard: *.qmachina.com

Install in Windows VM: Local Machine → Personal (My) store
Get thumbprint and update appsettings.json
```

### 3. Azure Bot Calling Webhook (One-time setup)
```
Update in Azure Portal:
Azure Bot → Channels → Teams → Calling → Webhook
Set to: https://teamsbot.qmachina.com/api/calling
```

### 4. No ngrok Needed! ✅
```
Production deployment uses stable qmachina.com URLs
No tunneling software required
URLs never change
```

---

## Next Steps

1. **Wait for Azure resources to finish creating** (~5-10 minutes)
2. **Get Speech Service keys** (will update CONFIG.md when ready)
3. **Set up DNS** (create CNAME at your provider)
4. **Get SSL certificate** (Let's Encrypt, commercial CA, or corporate cert)
5. **Set up Windows VM** (local Parallels or Azure VM)
6. **Transfer code** (Git push/pull or shared folder)
7. **Install certificate on Windows**
8. **Start ngrok**
9. **Update appsettings.json** with final values
10. **Build and run in Visual Studio**
