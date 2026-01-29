# Quick Debugging Commands Reference

All commands I can run remotely without RDP.

---

## 🏥 Health Check

### Quick Status
```bash
# Check if bot is responding
curl https://teamsbot.qmachina.com/api/calling/health
```

### Full System Diagnostics
```bash
# Run comprehensive diagnostics
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts @diagnose-bot.ps1
```

---

## 📋 View Logs

### Service Output (Normal Logs)
```bash
# Last 50 lines
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-Content C:\teams-bot-poc\logs\service-output.log -Tail 50"

# Last 100 lines
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-Content C:\teams-bot-poc\logs\service-output.log -Tail 100"
```

### Error Logs
```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-Content C:\teams-bot-poc\logs\service-error.log -Tail 50"
```

### Search Logs
```bash
# Find specific text
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-Content C:\teams-bot-poc\logs\service-output.log | Select-String 'ERROR_TEXT'"

# Examples:
# Search for errors: Select-String 'error|exception'
# Search for audio: Select-String 'Audio stats'
# Search for calls: Select-String 'Call created|Call state'
# Search for speech: Select-String 'Speech|Recogniz'
```

---

## 🔄 Service Management

### Check Service Status
```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-Service TeamsMediaBot | Select Status, StartType, DisplayName"
```

### Restart Service
```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Restart-Service TeamsMediaBot; Start-Sleep 3; Get-Service TeamsMediaBot"
```

### Stop Service
```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Stop-Service TeamsMediaBot; Get-Service TeamsMediaBot"
```

### Start Service
```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Start-Service TeamsMediaBot; Start-Sleep 3; Get-Service TeamsMediaBot"
```

---

## 🔧 Configuration Checks

### View Current Config
```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-Content C:\teams-bot-poc\src\Config\appsettings.json"
```

### Check Certificate
```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-ChildItem Cert:\LocalMachine\My | Where-Object {$_.Subject -like '*qmachina*'} | Select Thumbprint, Subject, NotAfter"
```

### Check DNS Resolution
```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Resolve-DnsName teamsbot.qmachina.com; Resolve-DnsName media.qmachina.com"
```

---

## 🌐 Network Checks

### Check Listening Ports
```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-NetTCPConnection -LocalPort 443,8445 -State Listen -ErrorAction SilentlyContinue | Select LocalAddress, LocalPort, State"
```

### Check Firewall Rules
```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-NetFirewallRule -DisplayName 'Teams Bot*' | Select DisplayName, Enabled, Direction, Action"
```

### Test Azure Speech Connectivity
```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Test-NetConnection -ComputerName eastus.api.cognitive.microsoft.com -Port 443"
```

---

## 🚀 Code Updates

### Pull Latest Code and Rebuild
```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts @update-bot.ps1
```

### Just Build (No Pull)
```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "cd C:\teams-bot-poc\src; dotnet build --configuration Release"
```

### Check Git Status
```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "cd C:\teams-bot-poc; git status; git log -1 --oneline"
```

---

## 🔍 Specific Issue Debugging

### Bot Won't Start
```bash
# Check error log
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-Content C:\teams-bot-poc\logs\service-error.log -Tail 20"

# Check if ports are in use
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-NetTCPConnection -LocalPort 443 -ErrorAction SilentlyContinue | Select OwningProcess, State"
```

### No Audio Frames
```bash
# Check if media port is listening
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-NetTCPConnection -LocalPort 8445 -State Listen -ErrorAction SilentlyContinue"

# Search logs for media activity
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-Content C:\teams-bot-poc\logs\service-output.log | Select-String 'media|Audio stats' | Select-Object -Last 10"
```

### No Transcripts
```bash
# Check for audio frames
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-Content C:\teams-bot-poc\logs\service-output.log | Select-String 'Audio stats' | Select-Object -Last 5"

# Check for Speech errors
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-Content C:\teams-bot-poc\logs\service-output.log | Select-String 'Speech|Canceled|Error' | Select-Object -Last 10"
```

### Certificate Issues
```bash
# Check if cert exists
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Get-ChildItem Cert:\LocalMachine\My | Select Thumbprint, Subject, NotAfter | Format-Table"

# Check thumbprint in config
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "(Get-Content C:\teams-bot-poc\src\Config\appsettings.json | ConvertFrom-Json).MediaPlatformSettings.CertificateThumbprint"
```

---

## 💾 Backup Commands

### Backup Current Config
```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Copy-Item C:\teams-bot-poc\src\Config\appsettings.json C:\teams-bot-poc\src\Config\appsettings.backup-$(Get-Date -Format 'yyyy-MM-dd-HHmmss').json"
```

### Restore Config
```bash
az vm run-command invoke \
  --resource-group rg-teams-media-bot-poc \
  --name vm-teams-bot-prod \
  --command-id RunPowerShellScript \
  --scripts "Copy-Item C:\teams-bot-poc\src\Config\appsettings.backup-YYYY-MM-DD-HHMMSS.json C:\teams-bot-poc\src\Config\appsettings.json; Restart-Service TeamsMediaBot"
```

---

## 🎯 Quick Reference

**Most Common Commands:**

```bash
# 1. Check if everything is healthy
az vm run-command invoke ... --scripts @diagnose-bot.ps1

# 2. View recent logs
az vm run-command invoke ... --scripts "Get-Content C:\teams-bot-poc\logs\service-output.log -Tail 50"

# 3. Restart service
az vm run-command invoke ... --scripts "Restart-Service TeamsMediaBot"

# 4. Update code and restart
az vm run-command invoke ... --scripts @update-bot.ps1

# 5. Check service status
az vm run-command invoke ... --scripts "Get-Service TeamsMediaBot"
```

**Copy/paste template:**
```bash
export RG="rg-teams-media-bot-poc"
export VM="vm-teams-bot-prod"

az vm run-command invoke \
  --resource-group $RG \
  --name $VM \
  --command-id RunPowerShellScript \
  --scripts "YOUR_COMMAND_HERE"
```

---

**Save this file for quick reference during debugging!**
