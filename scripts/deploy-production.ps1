# =====================================================================
# Teams Media Bot - Production Deployment Script
# Deploys to Azure VM with qmachina.com domain (no ngrok)
# Run via: az vm run-command invoke
# =====================================================================

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Teams Media Bot - Production Deployment" -ForegroundColor Cyan
Write-Host "Using: qmachina.com (no ngrok)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Configuration
$projectRoot = "C:\teams-bot-poc"
$serviceName = "TeamsMediaBot"
$gitRepo = "https://github.com/YOUR-USERNAME/teams-bot-poc.git"  # Update this!

# Get VM public IP
Write-Host "Getting VM public IP..." -ForegroundColor Yellow
$publicIP = (Invoke-RestMethod -Uri "http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/publicIpAddress?api-version=2021-02-01&format=text" -Headers @{"Metadata"="true"})
Write-Host "VM Public IP: $publicIP" -ForegroundColor Green
Write-Host ""

# Install Chocolatey
Write-Host "Installing Chocolatey..." -ForegroundColor Yellow
if (!(Get-Command choco -ErrorAction SilentlyContinue)) {
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    $chocoScript = (New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1')
    Invoke-Expression $chocoScript
}

# Refresh PATH
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# Install required software
Write-Host "Installing required software..." -ForegroundColor Yellow
choco install git -y --force
choco install dotnet-sdk -y --force
choco install nssm -y --force  # For Windows Service creation

# Refresh PATH again
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# Clone or update repository
Write-Host "Cloning/updating repository..." -ForegroundColor Yellow
if (Test-Path $projectRoot) {
    Write-Host "Repository exists, pulling latest..." -ForegroundColor Gray
    cd $projectRoot
    git pull origin main
} else {
    Write-Host "Cloning repository..." -ForegroundColor Gray
    cd C:\
    git clone $gitRepo teams-bot-poc
}

# Update appsettings.json with production values
Write-Host "Updating appsettings.json..." -ForegroundColor Yellow
$appsettingsPath = "$projectRoot\src\Config\appsettings.json"
$appsettings = Get-Content $appsettingsPath | ConvertFrom-Json

# Update URLs to use qmachina.com
$appsettings.Bot.NotificationUrl = "https://teamsbot.qmachina.com/api/calling"
$appsettings.Bot.LocalHttpListenUrl = "http://0.0.0.0:443"
$appsettings.Bot.LocalHttpListenPort = 443

$appsettings.MediaPlatformSettings.ServiceFqdn = "media.qmachina.com"
$appsettings.MediaPlatformSettings.InstancePublicPort = 8445
$appsettings.MediaPlatformSettings.InstanceInternalPort = 8445

# Save updated config
$appsettings | ConvertTo-Json -Depth 10 | Set-Content $appsettingsPath

Write-Host "✅ Configuration updated for qmachina.com" -ForegroundColor Green

# Build the project
Write-Host "Building project..." -ForegroundColor Yellow
cd "$projectRoot\src"
dotnet restore
dotnet build --configuration Release

if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Build failed!" -ForegroundColor Red
    exit 1
}

Write-Host "✅ Build successful" -ForegroundColor Green

# Configure Windows Firewall
Write-Host "Configuring Windows Firewall..." -ForegroundColor Yellow
New-NetFirewallRule -DisplayName "Teams Bot HTTPS" -Direction Inbound -LocalPort 443 -Protocol TCP -Action Allow -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "Teams Bot Media" -Direction Inbound -LocalPort 8445 -Protocol TCP -Action Allow -ErrorAction SilentlyContinue
Write-Host "✅ Firewall rules created" -ForegroundColor Green

# Create Windows Service (auto-start on boot)
Write-Host "Creating Windows Service..." -ForegroundColor Yellow

# Stop existing service if running
if (Get-Service $serviceName -ErrorAction SilentlyContinue) {
    Write-Host "Stopping existing service..." -ForegroundColor Gray
    Stop-Service $serviceName -Force
    nssm remove $serviceName confirm
}

# Create new service
$exePath = "C:\Program Files\dotnet\dotnet.exe"
$appPath = "$projectRoot\src\bin\Release\net8.0\TeamsMediaBot.dll"
$workingDir = "$projectRoot\src"

nssm install $serviceName $exePath
nssm set $serviceName Application $exePath
nssm set $serviceName AppParameters "exec `"$appPath`""
nssm set $serviceName AppDirectory $workingDir
nssm set $serviceName DisplayName "Teams Media Bot"
nssm set $serviceName Description "Teams meeting transcription bot using qmachina.com"
nssm set $serviceName Start SERVICE_AUTO_START
nssm set $serviceName AppStdout "$projectRoot\logs\service-output.log"
nssm set $serviceName AppStderr "$projectRoot\logs\service-error.log"
nssm set $serviceName AppRotateFiles 1
nssm set $serviceName AppRotateBytes 10485760  # 10MB

Write-Host "✅ Windows Service created" -ForegroundColor Green

# Start the service
Write-Host "Starting service..." -ForegroundColor Yellow
Start-Service $serviceName
Start-Sleep 5

# Check service status
$service = Get-Service $serviceName
if ($service.Status -eq "Running") {
    Write-Host "✅ Service is running!" -ForegroundColor Green
} else {
    Write-Host "⚠️  Service status: $($service.Status)" -ForegroundColor Yellow
    Write-Host "Check logs at: $projectRoot\logs\" -ForegroundColor Gray
}

# Display summary
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "✅ DEPLOYMENT COMPLETE!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "📊 Configuration:" -ForegroundColor Yellow
Write-Host "  VM Public IP: $publicIP" -ForegroundColor White
Write-Host "  Signaling: https://teamsbot.qmachina.com/api/calling" -ForegroundColor White
Write-Host "  Media: media.qmachina.com:8445" -ForegroundColor White
Write-Host ""
Write-Host "📝 DNS Records Needed:" -ForegroundColor Yellow
Write-Host "  teamsbot.qmachina.com → A → $publicIP" -ForegroundColor White
Write-Host "  media.qmachina.com → A → $publicIP" -ForegroundColor White
Write-Host ""
Write-Host "🔒 SSL Certificates Needed:" -ForegroundColor Yellow
Write-Host "  1. teamsbot.qmachina.com (HTTPS signaling)" -ForegroundColor White
Write-Host "  2. media.qmachina.com (Media endpoint)" -ForegroundColor White
Write-Host ""
Write-Host "⚠️  NEXT STEPS:" -ForegroundColor Yellow
Write-Host "  1. Create DNS A records (see above)" -ForegroundColor White
Write-Host "  2. Install SSL certificates" -ForegroundColor White
Write-Host "  3. Update certificate thumbprint in appsettings.json" -ForegroundColor White
Write-Host "  4. Restart service: Restart-Service $serviceName" -ForegroundColor White
Write-Host "  5. Update Azure Bot webhook to: https://teamsbot.qmachina.com/api/calling" -ForegroundColor White
Write-Host ""
Write-Host "📖 Service Management:" -ForegroundColor Yellow
Write-Host "  Status: Get-Service $serviceName" -ForegroundColor Gray
Write-Host "  Start: Start-Service $serviceName" -ForegroundColor Gray
Write-Host "  Stop: Stop-Service $serviceName" -ForegroundColor Gray
Write-Host "  Restart: Restart-Service $serviceName" -ForegroundColor Gray
Write-Host "  Logs: Get-Content $projectRoot\logs\service-output.log -Wait" -ForegroundColor Gray
Write-Host ""
Write-Host "🔗 Health Check:" -ForegroundColor Yellow
Write-Host "  https://teamsbot.qmachina.com/api/calling/health" -ForegroundColor Gray
Write-Host ""
