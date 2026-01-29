# =====================================================================
# Quick Update & Restart Script
# Run remotely via: az vm run-command invoke --scripts @update-bot.ps1
# =====================================================================

$ErrorActionPreference = "Stop"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Updating Teams Media Bot" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Pull latest code
Write-Host "Pulling latest code from GitHub..." -ForegroundColor Yellow
cd C:\teams-bot-poc
git pull origin main

if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Git pull failed!" -ForegroundColor Red
    exit 1
}

Write-Host "✅ Code updated" -ForegroundColor Green

# Build project
Write-Host "Building project..." -ForegroundColor Yellow
cd src
dotnet build --configuration Release

if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Build failed!" -ForegroundColor Red
    Get-Content C:\teams-bot-poc\logs\build-error.log -ErrorAction SilentlyContinue
    exit 1
}

Write-Host "✅ Build successful" -ForegroundColor Green

# Restart service
Write-Host "Restarting service..." -ForegroundColor Yellow
Restart-Service TeamsMediaBot -ErrorAction Stop

Write-Host "✅ Service restarted" -ForegroundColor Green

# Wait for startup
Write-Host "Waiting for service to start..." -ForegroundColor Gray
Start-Sleep 5

# Check status
$service = Get-Service TeamsMediaBot
Write-Host ""
Write-Host "Service Status: $($service.Status)" -ForegroundColor $(if ($service.Status -eq "Running") { "Green" } else { "Red" })

# Show recent logs
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Recent Logs (last 20 lines):" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Get-Content C:\teams-bot-poc\logs\service-output.log -Tail 20 -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "✅ Update complete!" -ForegroundColor Green
Write-Host "To view live logs: Get-Content C:\teams-bot-poc\logs\service-output.log -Wait" -ForegroundColor Gray
