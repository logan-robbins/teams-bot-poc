# =====================================================================
# Remote Windows Server Setup and Run Script
# Designed to run via az vm run-command invoke (no GUI required)
# =====================================================================

# Enable error handling
$ErrorActionPreference = "Stop"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Teams Media Bot - Automated Setup & Run" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Configuration
$projectRoot = "C:\teams-bot-poc"
$logsDir = "$projectRoot\logs"
$certPassword = "BotCert2026!"  # Change this!

# Create directories
Write-Host "Creating directories..." -ForegroundColor Yellow
New-Item -ItemType Directory -Path $projectRoot -Force | Out-Null
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null

# Install Chocolatey if not present
Write-Host "Checking Chocolatey..." -ForegroundColor Yellow
if (!(Get-Command choco -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Chocolatey..." -ForegroundColor Yellow
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
}

# Install required software
Write-Host "Installing software..." -ForegroundColor Yellow
choco install git -y --force --no-progress
choco install dotnet-sdk -y --force --no-progress
choco install ngrok -y --force --no-progress

# Refresh environment variables
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# Clone repository (if not already cloned)
Write-Host "Cloning repository..." -ForegroundColor Yellow
if (!(Test-Path "$projectRoot\.git")) {
    cd C:\
    git clone https://github.com/YOUR-USERNAME/teams-bot-poc.git teams-bot-poc
} else {
    cd $projectRoot
    git pull origin main
}

cd $projectRoot

# Build the project
Write-Host "Building project..." -ForegroundColor Yellow
cd src
dotnet restore
dotnet build --configuration Release

# Run the bot (in background, logs to file)
Write-Host "Starting bot..." -ForegroundColor Yellow
$process = Start-Process -FilePath "dotnet" `
    -ArgumentList "run --configuration Release --no-build" `
    -WorkingDirectory "$projectRoot\src" `
    -RedirectStandardOutput "$logsDir\bot-output.log" `
    -RedirectStandardError "$logsDir\bot-error.log" `
    -PassThru `
    -NoNewWindow

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "Bot started successfully!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host "Process ID: $($process.Id)" -ForegroundColor White
Write-Host "Output log: $logsDir\bot-output.log" -ForegroundColor White
Write-Host "Error log: $logsDir\bot-error.log" -ForegroundColor White
Write-Host ""
Write-Host "To view logs:" -ForegroundColor Yellow
Write-Host "  Get-Content $logsDir\bot-output.log -Wait" -ForegroundColor Gray
Write-Host ""
Write-Host "To check if running:" -ForegroundColor Yellow
Write-Host "  Get-Process -Id $($process.Id)" -ForegroundColor Gray
Write-Host ""

# Return process info
@{
    ProcessId = $process.Id
    OutputLog = "$logsDir\bot-output.log"
    ErrorLog = "$logsDir\bot-error.log"
    Status = "Running"
} | ConvertTo-Json
