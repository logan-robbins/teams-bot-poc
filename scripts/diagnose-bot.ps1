# =====================================================================
# Bot Diagnostics Script
# Run remotely to check system health
# =====================================================================

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Teams Media Bot - System Diagnostics" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$results = @()

# Check Windows Service
Write-Host "1. Checking Windows Service..." -ForegroundColor Yellow
$service = Get-Service TeamsMediaBot -ErrorAction SilentlyContinue
if ($service) {
    $status = if ($service.Status -eq "Running") { "✅" } else { "❌" }
    Write-Host "   $status Service Status: $($service.Status)" -ForegroundColor $(if ($service.Status -eq "Running") { "Green" } else { "Red" })
    $results += "Service: $($service.Status)"
} else {
    Write-Host "   ❌ Service not found!" -ForegroundColor Red
    $results += "Service: Not Found"
}

# Check if ports are listening
Write-Host ""
Write-Host "2. Checking Network Ports..." -ForegroundColor Yellow
$port443 = Get-NetTCPConnection -LocalPort 443 -State Listen -ErrorAction SilentlyContinue
$port8445 = Get-NetTCPConnection -LocalPort 8445 -State Listen -ErrorAction SilentlyContinue

if ($port443) {
    Write-Host "   ✅ Port 443 (HTTPS) listening" -ForegroundColor Green
    $results += "Port 443: Listening"
} else {
    Write-Host "   ❌ Port 443 not listening!" -ForegroundColor Red
    $results += "Port 443: Not Listening"
}

if ($port8445) {
    Write-Host "   ✅ Port 8445 (Media) listening" -ForegroundColor Green
    $results += "Port 8445: Listening"
} else {
    Write-Host "   ❌ Port 8445 not listening!" -ForegroundColor Red
    $results += "Port 8445: Not Listening"
}

# Check DNS resolution
Write-Host ""
Write-Host "3. Checking DNS Resolution..." -ForegroundColor Yellow
try {
    $dns1 = Resolve-DnsName teamsbot.qmachina.com -ErrorAction Stop
    Write-Host "   ✅ teamsbot.qmachina.com resolves to: $($dns1.IPAddress)" -ForegroundColor Green
    $results += "DNS teamsbot: OK"
} catch {
    Write-Host "   ❌ teamsbot.qmachina.com not resolving!" -ForegroundColor Red
    $results += "DNS teamsbot: Failed"
}

try {
    $dns2 = Resolve-DnsName media.qmachina.com -ErrorAction Stop
    Write-Host "   ✅ media.qmachina.com resolves to: $($dns2.IPAddress)" -ForegroundColor Green
    $results += "DNS media: OK"
} catch {
    Write-Host "   ❌ media.qmachina.com not resolving!" -ForegroundColor Red
    $results += "DNS media: Failed"
}

# Check certificate
Write-Host ""
Write-Host "4. Checking SSL Certificate..." -ForegroundColor Yellow
$certs = Get-ChildItem Cert:\LocalMachine\My | Where-Object {$_.Subject -like "*qmachina*"}
if ($certs) {
    foreach ($cert in $certs) {
        $expired = $cert.NotAfter -lt (Get-Date)
        $status = if ($expired) { "❌ EXPIRED" } else { "✅" }
        Write-Host "   $status Subject: $($cert.Subject)" -ForegroundColor $(if ($expired) { "Red" } else { "Green" })
        Write-Host "      Thumbprint: $($cert.Thumbprint)" -ForegroundColor Gray
        Write-Host "      Expires: $($cert.NotAfter)" -ForegroundColor Gray
        $results += "Cert: $(if ($expired) { 'Expired' } else { 'Valid' })"
    }
} else {
    Write-Host "   ❌ No certificates found matching *qmachina*" -ForegroundColor Red
    $results += "Cert: Not Found"
}

# Check configuration file
Write-Host ""
Write-Host "5. Checking Configuration..." -ForegroundColor Yellow
$configPath = "C:\teams-bot-poc\src\Config\appsettings.json"
if (Test-Path $configPath) {
    $config = Get-Content $configPath | ConvertFrom-Json
    Write-Host "   ✅ Config file exists" -ForegroundColor Green
    Write-Host "      NotificationUrl: $($config.Bot.NotificationUrl)" -ForegroundColor Gray
    Write-Host "      ServiceFqdn: $($config.MediaPlatformSettings.ServiceFqdn)" -ForegroundColor Gray
    Write-Host "      CertThumbprint: $($config.MediaPlatformSettings.CertificateThumbprint.Substring(0,10))..." -ForegroundColor Gray
    $results += "Config: OK"
} else {
    Write-Host "   ❌ Config file not found!" -ForegroundColor Red
    $results += "Config: Not Found"
}

# Check connectivity to Azure Speech
Write-Host ""
Write-Host "6. Checking Azure Speech Service..." -ForegroundColor Yellow
try {
    $speechTest = Test-NetConnection -ComputerName eastus.api.cognitive.microsoft.com -Port 443 -WarningAction SilentlyContinue
    if ($speechTest.TcpTestSucceeded) {
        Write-Host "   ✅ Can reach Azure Speech Service" -ForegroundColor Green
        $results += "Speech Service: Reachable"
    } else {
        Write-Host "   ❌ Cannot reach Azure Speech Service!" -ForegroundColor Red
        $results += "Speech Service: Unreachable"
    }
} catch {
    Write-Host "   ❌ Error testing Speech Service: $($_.Exception.Message)" -ForegroundColor Red
    $results += "Speech Service: Error"
}

# Check recent logs for errors
Write-Host ""
Write-Host "7. Checking Recent Logs..." -ForegroundColor Yellow
$errorLog = "C:\teams-bot-poc\logs\service-error.log"
if (Test-Path $errorLog) {
    $errorContent = Get-Content $errorLog -Tail 5 -ErrorAction SilentlyContinue
    if ($errorContent) {
        Write-Host "   ⚠️  Recent errors found:" -ForegroundColor Yellow
        $errorContent | ForEach-Object { Write-Host "      $_" -ForegroundColor Gray }
        $results += "Errors: Yes"
    } else {
        Write-Host "   ✅ No recent errors" -ForegroundColor Green
        $results += "Errors: None"
    }
} else {
    Write-Host "   ⚠️  Error log not found" -ForegroundColor Yellow
    $results += "Errors: Log Missing"
}

# Check if bot is processing audio
Write-Host ""
Write-Host "8. Checking Audio Processing..." -ForegroundColor Yellow
$outputLog = "C:\teams-bot-poc\logs\service-output.log"
if (Test-Path $outputLog) {
    $audioStats = Get-Content $outputLog -Tail 100 -ErrorAction SilentlyContinue | Select-String "Audio stats"
    if ($audioStats) {
        Write-Host "   ✅ Audio frames detected in logs" -ForegroundColor Green
        Write-Host "      Latest: $($audioStats[-1])" -ForegroundColor Gray
        $results += "Audio: Processing"
    } else {
        Write-Host "   ⚠️  No audio frames in recent logs" -ForegroundColor Yellow
        $results += "Audio: No Activity"
    }
} else {
    Write-Host "   ⚠️  Output log not found" -ForegroundColor Yellow
    $results += "Audio: Log Missing"
}

# Summary
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Diagnostic Summary" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
$results | ForEach-Object { Write-Host "  $_" }

Write-Host ""
Write-Host "To view live logs:" -ForegroundColor Yellow
Write-Host "  Get-Content C:\teams-bot-poc\logs\service-output.log -Wait" -ForegroundColor Gray
