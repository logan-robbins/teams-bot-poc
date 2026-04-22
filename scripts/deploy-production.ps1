# Deploy Teams Media Bot to Production (Standardized)
# Implements reliable clean/build/deploy cycle for Windows Server 2022

param(
    [string]$ProjectRoot = "C:\teams-bot-poc",
    [string]$RepoUrl = "https://github.com/logan-robbins/teams-bot-poc.git",
    [string]$RepoBranch = "main"
)

$ErrorActionPreference = "Stop"
$RepoRoot = $ProjectRoot
$SrcDir = Join-Path $ProjectRoot "src"
$BinDir = Join-Path $SrcDir "bin"
$ObjDir = Join-Path $SrcDir "obj"
$ReleaseDir = Join-Path $BinDir "Release\net8.0"

Write-Host "🚀 Starting Standardized Deployment..." -ForegroundColor Cyan

# 0. Ensure repository is present and current
Write-Host "📦 Ensuring repository is available..." -ForegroundColor Yellow
$GitDir = Join-Path $RepoRoot ".git"
if (-not (Test-Path $GitDir)) {
    $RepoParent = Split-Path -Parent $RepoRoot
    if (-not (Test-Path $RepoParent)) {
        New-Item -ItemType Directory -Path $RepoParent -Force | Out-Null
    }

    git clone --branch $RepoBranch --single-branch $RepoUrl $RepoRoot
}
else {
    Push-Location $RepoRoot
    try {
        git fetch origin $RepoBranch
        git checkout $RepoBranch
        git pull --ff-only origin $RepoBranch
    } finally {
        Pop-Location
    }
}

# 1. Stop Service and Ensure Process Termination
Write-Host "🛑 Stopping service and ensuring process termination..." -ForegroundColor Yellow
try {
    Stop-Service TeamsMediaBot -Force -ErrorAction SilentlyContinue
} catch {
    Write-Host "Service stop warning: $_" -ForegroundColor DarkGray
}

# Kill any lingering processes to release file locks
taskkill /F /IM TeamsMediaBot.exe /T 2>$null
taskkill /F /IM dotnet.exe /T 2>$null
Start-Sleep -Seconds 3

# 2. Clean Build Artifacts
Write-Host "🧹 Cleaning build artifacts..." -ForegroundColor Yellow
Push-Location $SrcDir
try {
    dotnet clean --configuration Release
    if (Test-Path $BinDir) { Remove-Item -Path $BinDir -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path $ObjDir) { Remove-Item -Path $ObjDir -Recurse -Force -ErrorAction SilentlyContinue }
} finally {
    Pop-Location
}

# 3. Build Release
Write-Host "🔨 Building Release configuration..." -ForegroundColor Yellow
Push-Location $SrcDir
try {
    dotnet restore
    dotnet build --configuration Release
} finally {
    Pop-Location
}

# 4. Verify Critical Assets
Write-Host "🔍 Verifying deployment artifacts..." -ForegroundColor Yellow
$NativeDll = Join-Path $ReleaseDir "NativeMedia.dll"
if (-not (Test-Path $NativeDll)) {
    Write-Error "❌ CRITICAL: NativeMedia.dll is missing from output directory. Deployment failed."
}
Write-Host "✅ NativeMedia.dll found." -ForegroundColor Green

# 5. Verify Service Configuration (Idempotent)
Write-Host "👤 Verifying service account..." -ForegroundColor Yellow
$ServiceConfig = nssm get TeamsMediaBot ObjectName
if ($ServiceConfig -notmatch "azureuser") {
    Write-Host "⚠️ Service not running as azureuser. Fixing..." -ForegroundColor Red
    nssm set TeamsMediaBot ObjectName ".\azureuser" "SecureTeamsBot2026!"
} else {
    Write-Host "✅ Service configured to run as azureuser." -ForegroundColor Green
}

# 6. Start Service
Write-Host "▶️ Starting service..." -ForegroundColor Yellow
Start-Service TeamsMediaBot

# 7. Validate Health
Write-Host "🏥 Validating health..." -ForegroundColor Yellow
Start-Sleep -Seconds 5
$ServiceStatus = Get-Service TeamsMediaBot
Write-Host "Service Status: $($ServiceStatus.Status)" -ForegroundColor Cyan

if ($ServiceStatus.Status -eq "Running") {
    Write-Host "✅ Deployment Complete & Service Running" -ForegroundColor Green
} else {
    Write-Error "❌ Service failed to start. Check logs at C:\teams-bot-poc\logs"
}
