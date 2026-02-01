<#
.SYNOPSIS
    PowerShell wrapper for Teams Interview Bot auto-join script.

.DESCRIPTION
    Wraps the Python auto_join.py script for Windows Task Scheduler integration.
    Logs output to a file and handles errors gracefully.
    
    Last Grunted: 01/31/2026

.PARAMETER MeetingUrl
    Teams meeting join URL (required)

.PARAMETER CandidateName
    Name of the candidate being interviewed (required)

.PARAMETER BotEndpoint
    Bot API endpoint (default: https://teamsbot.qmachina.com)

.PARAMETER SinkEndpoint
    Python sink endpoint (default: http://127.0.0.1:8765)

.PARAMETER DisplayName
    Bot display name in meeting (default: Talestral by Talestry)

.PARAMETER DryRun
    Print what would be done without making actual requests

.PARAMETER LogFile
    Path to log file (default: C:\teams-bot-poc\logs\auto_join.log)

.EXAMPLE
    .\auto_join.ps1 -MeetingUrl "https://teams.microsoft.com/l/meetup-join/..." -CandidateName "John Doe"

.EXAMPLE
    .\auto_join.ps1 -MeetingUrl "https://teams.microsoft.com/l/meetup-join/..." -CandidateName "Jane Smith" -DryRun

.EXAMPLE
    # For Task Scheduler, use:
    powershell.exe -ExecutionPolicy Bypass -File "C:\teams-bot-poc\scripts\auto_join.ps1" -MeetingUrl "..." -CandidateName "..."

.NOTES
    Requires Python 3.11+ and uv package manager installed.
    Assumes the teams-bot-poc repository is at C:\teams-bot-poc
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true, HelpMessage="Teams meeting join URL")]
    [string]$MeetingUrl,

    [Parameter(Mandatory=$true, HelpMessage="Candidate name")]
    [string]$CandidateName,

    [Parameter(Mandatory=$false)]
    [string]$BotEndpoint = "https://teamsbot.qmachina.com",

    [Parameter(Mandatory=$false)]
    [string]$SinkEndpoint = "https://agent.qmachina.com",

    [Parameter(Mandatory=$false)]
    [string]$DisplayName = "Talestral by Talestry",

    [Parameter(Mandatory=$false)]
    [switch]$DryRun,

    [Parameter(Mandatory=$false)]
    [string]$LogFile = "C:\teams-bot-poc\logs\auto_join.log"
)

# Configuration
$ErrorActionPreference = "Stop"
$RepoRoot = "C:\teams-bot-poc"
$PythonScript = Join-Path $RepoRoot "scripts\auto_join.py"

# Ensure log directory exists
$LogDir = Split-Path $LogFile -Parent
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

function Write-Log {
    param(
        [Parameter(Mandatory=$true)]
        [string]$Message,
        
        [Parameter(Mandatory=$false)]
        [ValidateSet("INFO", "WARN", "ERROR")]
        [string]$Level = "INFO"
    )
    
    $Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $LogEntry = "[$Timestamp] [$Level] $Message"
    
    # Write to console
    switch ($Level) {
        "ERROR" { Write-Host $LogEntry -ForegroundColor Red }
        "WARN"  { Write-Host $LogEntry -ForegroundColor Yellow }
        default { Write-Host $LogEntry }
    }
    
    # Append to log file
    Add-Content -Path $LogFile -Value $LogEntry
}

function Test-Prerequisites {
    Write-Log "Checking prerequisites..."
    
    # Check if uv is available
    $uvPath = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uvPath) {
        Write-Log "uv package manager not found. Please install: https://docs.astral.sh/uv/getting-started/installation/" -Level ERROR
        return $false
    }
    Write-Log "Found uv at: $($uvPath.Source)"
    
    # Check if Python script exists
    if (-not (Test-Path $PythonScript)) {
        Write-Log "Python script not found: $PythonScript" -Level ERROR
        return $false
    }
    Write-Log "Found Python script: $PythonScript"
    
    # Check if repository exists
    if (-not (Test-Path $RepoRoot)) {
        Write-Log "Repository not found: $RepoRoot" -Level ERROR
        return $false
    }
    
    return $true
}

function Invoke-AutoJoin {
    Write-Log "=========================================="
    Write-Log "Talestral by Talestry - Auto Join (PowerShell)"
    Write-Log "=========================================="
    Write-Log "Meeting URL: $MeetingUrl"
    Write-Log "Candidate: $CandidateName"
    Write-Log "Bot Endpoint: $BotEndpoint"
    Write-Log "Sink Endpoint: $SinkEndpoint"
    Write-Log "Display Name: $DisplayName"
    if ($DryRun) {
        Write-Log "MODE: DRY RUN"
    }
    Write-Log "------------------------------------------"
    
    # Build command arguments
    $Arguments = @(
        "run",
        "python",
        $PythonScript,
        "--meeting-url", "`"$MeetingUrl`"",
        "--candidate-name", "`"$CandidateName`"",
        "--bot-endpoint", $BotEndpoint,
        "--sink-endpoint", $SinkEndpoint,
        "--display-name", "`"$DisplayName`""
    )
    
    if ($DryRun) {
        $Arguments += "--dry-run"
    }
    
    Write-Log "Executing: uv $($Arguments -join ' ')"
    
    try {
        # Change to repository directory
        Push-Location $RepoRoot
        
        # Execute the Python script via uv
        $Process = Start-Process -FilePath "uv" `
            -ArgumentList $Arguments `
            -WorkingDirectory $RepoRoot `
            -NoNewWindow `
            -PassThru `
            -Wait `
            -RedirectStandardOutput "$LogDir\auto_join_stdout.tmp" `
            -RedirectStandardError "$LogDir\auto_join_stderr.tmp"
        
        # Read and log output
        if (Test-Path "$LogDir\auto_join_stdout.tmp") {
            $StdOut = Get-Content "$LogDir\auto_join_stdout.tmp" -Raw
            if ($StdOut) {
                $StdOut -split "`n" | ForEach-Object { Write-Log $_ }
            }
            Remove-Item "$LogDir\auto_join_stdout.tmp" -Force -ErrorAction SilentlyContinue
        }
        
        if (Test-Path "$LogDir\auto_join_stderr.tmp") {
            $StdErr = Get-Content "$LogDir\auto_join_stderr.tmp" -Raw
            if ($StdErr) {
                $StdErr -split "`n" | ForEach-Object { Write-Log $_ -Level WARN }
            }
            Remove-Item "$LogDir\auto_join_stderr.tmp" -Force -ErrorAction SilentlyContinue
        }
        
        # Check exit code
        if ($Process.ExitCode -eq 0) {
            Write-Log "Auto-join completed successfully"
            return $true
        } else {
            Write-Log "Auto-join failed with exit code: $($Process.ExitCode)" -Level ERROR
            return $false
        }
    }
    catch {
        Write-Log "Exception during auto-join: $_" -Level ERROR
        return $false
    }
    finally {
        Pop-Location
    }
}

# Main execution
try {
    Write-Log "Starting auto-join process..."
    
    # Check prerequisites
    if (-not (Test-Prerequisites)) {
        Write-Log "Prerequisites check failed. Exiting." -Level ERROR
        exit 1
    }
    
    # Run auto-join
    $Success = Invoke-AutoJoin
    
    if ($Success) {
        Write-Log "=========================================="
        Write-Log "Auto-join process completed successfully"
        Write-Log "=========================================="
        exit 0
    } else {
        Write-Log "=========================================="
        Write-Log "Auto-join process failed" -Level ERROR
        Write-Log "=========================================="
        exit 1
    }
}
catch {
    Write-Log "Unhandled exception: $_" -Level ERROR
    Write-Log $_.ScriptStackTrace -Level ERROR
    exit 1
}
