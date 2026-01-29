# =====================================================================
# Windows VM Setup Script
# Teams Media Bot POC
# Run this on your Windows VM (local or Azure)
# =====================================================================

# Require admin privileges
#Requires -RunAsAdministrator

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Teams Media Bot POC - Windows Setup" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Configuration
$projectRoot = "C:\dev\teams-bot-poc"
$ngrokPath = "C:\ngrok"

# Check if running as admin
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "❌ This script must be run as Administrator" -ForegroundColor Red
    exit 1
}

Write-Host "✅ Running as Administrator" -ForegroundColor Green
Write-Host ""

# Check if .NET SDK is installed
Write-Host "Checking .NET SDK..." -ForegroundColor Yellow
$dotnetVersion = & dotnet --version 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "✅ .NET SDK installed: $dotnetVersion" -ForegroundColor Green
} else {
    Write-Host "❌ .NET SDK not found" -ForegroundColor Red
    Write-Host "Download from: https://dotnet.microsoft.com/download" -ForegroundColor Yellow
    Write-Host "Install .NET 8.0 SDK or later" -ForegroundColor Yellow
    exit 1
}

# Check if Visual Studio is installed
Write-Host ""
Write-Host "Checking Visual Studio..." -ForegroundColor Yellow
$vsPath = "C:\Program Files\Microsoft Visual Studio\2022"
if (Test-Path $vsPath) {
    Write-Host "✅ Visual Studio 2022 found" -ForegroundColor Green
} else {
    Write-Host "⚠️  Visual Studio 2022 not found at default location" -ForegroundColor Yellow
    Write-Host "Install from: https://visualstudio.microsoft.com/" -ForegroundColor Yellow
}

# Check if ngrok is installed
Write-Host ""
Write-Host "Checking ngrok..." -ForegroundColor Yellow
$ngrokExe = Get-Command ngrok -ErrorAction SilentlyContinue
if ($ngrokExe) {
    Write-Host "✅ ngrok found: $($ngrokExe.Source)" -ForegroundColor Green
} else {
    Write-Host "⚠️  ngrok not found" -ForegroundColor Yellow
    Write-Host "Download from: https://ngrok.com/download" -ForegroundColor Yellow
    Write-Host "Install to: $ngrokPath" -ForegroundColor Yellow
}

# Check if Git is installed
Write-Host ""
Write-Host "Checking Git..." -ForegroundColor Yellow
$gitVersion = & git --version 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "✅ Git installed: $gitVersion" -ForegroundColor Green
} else {
    Write-Host "⚠️  Git not found" -ForegroundColor Yellow
    Write-Host "Download from: https://git-scm.com/download/win" -ForegroundColor Yellow
}

# List installed certificates
Write-Host ""
Write-Host "Checking installed certificates in LocalMachine\My..." -ForegroundColor Yellow
$certs = Get-ChildItem Cert:\LocalMachine\My | Sort-Object NotAfter -Descending | Select-Object -First 10
if ($certs.Count -gt 0) {
    Write-Host "✅ Found $($certs.Count) certificates:" -ForegroundColor Green
    $certs | ForEach-Object {
        Write-Host "  - Subject: $($_.Subject)" -ForegroundColor Gray
        Write-Host "    Thumbprint: $($_.Thumbprint)" -ForegroundColor Gray
        Write-Host "    Expires: $($_.NotAfter)" -ForegroundColor Gray
        Write-Host ""
    }
} else {
    Write-Host "⚠️  No certificates found in LocalMachine\My" -ForegroundColor Yellow
    Write-Host "You need to install your wildcard certificate (*.botpoc.YOURDOMAIN.com)" -ForegroundColor Yellow
}

# Check if project directory exists
Write-Host ""
Write-Host "Checking project directory..." -ForegroundColor Yellow
if (Test-Path $projectRoot) {
    Write-Host "✅ Project directory exists: $projectRoot" -ForegroundColor Green
} else {
    Write-Host "⚠️  Project directory not found: $projectRoot" -ForegroundColor Yellow
    Write-Host ""
    $createDir = Read-Host "Create directory and clone repo? (y/n)"
    if ($createDir -eq 'y') {
        New-Item -ItemType Directory -Path $projectRoot -Force | Out-Null
        Write-Host "✅ Directory created" -ForegroundColor Green
        Write-Host ""
        Write-Host "Clone your Git repository now:" -ForegroundColor Yellow
        Write-Host "  cd $projectRoot" -ForegroundColor Gray
        Write-Host "  git clone <YOUR-REPO-URL> ." -ForegroundColor Gray
    }
}

# Check if appsettings.json is configured
Write-Host ""
Write-Host "Checking appsettings.json..." -ForegroundColor Yellow
$appsettingsPath = Join-Path $projectRoot "src\Config\appsettings.json"
if (Test-Path $appsettingsPath) {
    $appsettings = Get-Content $appsettingsPath | ConvertFrom-Json
    
    $needsConfig = $false
    
    if ($appsettings.Bot.NotificationUrl -like "*CHANGE_ME*") {
        Write-Host "⚠️  NotificationUrl needs to be updated (ngrok URL)" -ForegroundColor Yellow
        $needsConfig = $true
    }
    
    if ($appsettings.MediaPlatformSettings.CertificateThumbprint -like "*CHANGE*") {
        Write-Host "⚠️  CertificateThumbprint needs to be updated" -ForegroundColor Yellow
        $needsConfig = $true
    }
    
    if ($appsettings.MediaPlatformSettings.ServiceFqdn -like "*YOURDOMAIN*") {
        Write-Host "⚠️  ServiceFqdn needs to be updated (your DNS)" -ForegroundColor Yellow
        $needsConfig = $true
    }
    
    if ($appsettings.Speech.Key -like "*PENDING*") {
        Write-Host "⚠️  Speech Key needs to be updated" -ForegroundColor Yellow
        $needsConfig = $true
    }
    
    if (-not $needsConfig) {
        Write-Host "✅ appsettings.json looks configured" -ForegroundColor Green
    }
} else {
    Write-Host "⚠️  appsettings.json not found" -ForegroundColor Yellow
}

# Check if ngrok config exists
Write-Host ""
Write-Host "Checking ngrok configuration..." -ForegroundColor Yellow
$ngrokConfigPath = Join-Path $projectRoot "scripts\ngrok.yml"
if (Test-Path $ngrokConfigPath) {
    $ngrokConfig = Get-Content $ngrokConfigPath -Raw
    if ($ngrokConfig -like "*YOUR_NGROK_AUTHTOKEN*") {
        Write-Host "⚠️  ngrok.yml needs authtoken configured" -ForegroundColor Yellow
    } else {
        Write-Host "✅ ngrok.yml found and configured" -ForegroundColor Green
    }
} else {
    Write-Host "⚠️  ngrok.yml not found at $ngrokConfigPath" -ForegroundColor Yellow
}

# Build the project if it exists
Write-Host ""
Write-Host "Checking if project builds..." -ForegroundColor Yellow
$csprojPath = Join-Path $projectRoot "src\TeamsMediaBot.csproj"
if (Test-Path $csprojPath) {
    Write-Host "Building project..." -ForegroundColor Yellow
    Push-Location (Split-Path $csprojPath -Parent)
    $buildResult = & dotnet build --configuration Release 2>&1
    Pop-Location
    
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✅ Project builds successfully" -ForegroundColor Green
    } else {
        Write-Host "❌ Build failed" -ForegroundColor Red
        Write-Host $buildResult -ForegroundColor Gray
    }
} else {
    Write-Host "⚠️  Project file not found: $csprojPath" -ForegroundColor Yellow
}

# Summary
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Setup Check Complete!" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "📋 Next Steps:" -ForegroundColor Yellow
Write-Host ""
Write-Host "1. Install missing prerequisites (if any listed above)" -ForegroundColor White
Write-Host ""
Write-Host "2. Install your wildcard SSL certificate:" -ForegroundColor White
Write-Host "   - Open mmc.exe" -ForegroundColor Gray
Write-Host "   - Add Certificates snap-in (Computer account, Local computer)" -ForegroundColor Gray
Write-Host "   - Import PFX to Personal → Certificates" -ForegroundColor Gray
Write-Host "   - Copy the thumbprint" -ForegroundColor Gray
Write-Host ""
Write-Host "3. Update appsettings.json:" -ForegroundColor White
Write-Host "   - CertificateThumbprint (from step 2)" -ForegroundColor Gray
Write-Host "   - ServiceFqdn (your DNS: 0.botpoc.YOURDOMAIN.com)" -ForegroundColor Gray
Write-Host "   - NotificationUrl (ngrok HTTPS URL)" -ForegroundColor Gray
Write-Host "   - InstancePublicPort (ngrok TCP port)" -ForegroundColor Gray
Write-Host "   - Speech.Key (from Azure setup)" -ForegroundColor Gray
Write-Host ""
Write-Host "4. Update scripts\ngrok.yml with your ngrok authtoken" -ForegroundColor White
Write-Host ""
Write-Host "5. Start ngrok:" -ForegroundColor White
Write-Host "   ngrok start --all --config $projectRoot\scripts\ngrok.yml" -ForegroundColor Gray
Write-Host ""
Write-Host "6. Update Azure Bot calling webhook in Azure Portal" -ForegroundColor White
Write-Host ""
Write-Host "7. Open solution in Visual Studio:" -ForegroundColor White
Write-Host "   $projectRoot\src\TeamsMediaBot.csproj" -ForegroundColor Gray
Write-Host ""
Write-Host "8. Run (F5) in Visual Studio" -ForegroundColor White
Write-Host ""
Write-Host "See SETUP-GUIDE.md for detailed instructions!" -ForegroundColor Yellow
Write-Host ""
