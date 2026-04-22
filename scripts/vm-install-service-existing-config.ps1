param(
    [string]$ProjectRoot = "C:\teams-bot-poc",
    [string]$ConfigPath = "C:\teams-bot-poc\src\Config\appsettings.production.json",
    [string]$RunAsUser = "azureuser",
    [string]$RunAsPassword
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RunAsPassword)) {
    throw "RunAsPassword is required."
}

if (-not (Test-Path $ConfigPath)) {
    throw "Production config not found at '$ConfigPath'."
}

$config = Get-Content -Path $ConfigPath -Raw | ConvertFrom-Json

$cert = Get-ChildItem Cert:\LocalMachine\My |
    Where-Object {
        $_.Subject -match "CN=teamsbot\.qmachina\.com" -or
        $_.FriendlyName -like "qmachina-teamsbot-media*"
    } |
    Sort-Object NotAfter -Descending |
    Select-Object -First 1

if (-not $cert) {
    throw "No bot certificate found in Cert:\LocalMachine\My."
}

$config.MediaPlatformSettings.CertificateThumbprint = $cert.Thumbprint
$config | ConvertTo-Json -Depth 8 | Set-Content -Path $ConfigPath -Encoding UTF8

$publishDir = Join-Path $ProjectRoot "src\bin\Release\net8.0\publish"
$exePath = Join-Path $publishDir "TeamsMediaBot.exe"
$logsDir = Join-Path $ProjectRoot "logs"
$stdoutLog = Join-Path $logsDir "service-output.log"
$stderrLog = Join-Path $logsDir "service-error.log"

if (-not (Test-Path $exePath)) {
    throw "Published executable not found at '$exePath'."
}

if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
}

if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    $nssmPath = "C:\ProgramData\chocolatey\bin\nssm.exe"
    if (Test-Path $nssmPath) {
        $env:Path = "C:\ProgramData\chocolatey\bin;$env:Path"
    }
    else {
        throw "nssm is not installed."
    }
}

$service = Get-Service -Name "TeamsMediaBot" -ErrorAction SilentlyContinue
if (-not $service) {
    nssm install TeamsMediaBot $exePath "--config $ConfigPath"
}
else {
    try {
        Stop-Service TeamsMediaBot -Force -ErrorAction SilentlyContinue
    }
    catch {
    }
}

nssm set TeamsMediaBot Application $exePath
nssm set TeamsMediaBot AppDirectory $publishDir
nssm set TeamsMediaBot AppParameters "--config $ConfigPath"
nssm set TeamsMediaBot Start SERVICE_AUTO_START
nssm set TeamsMediaBot AppStdout $stdoutLog
nssm set TeamsMediaBot AppStderr $stderrLog
nssm set TeamsMediaBot ObjectName ".\$RunAsUser" $RunAsPassword

Start-Service TeamsMediaBot
Start-Sleep -Seconds 8

$finalService = Get-Service -Name "TeamsMediaBot" -ErrorAction Stop
$tailStdout = if (Test-Path $stdoutLog) { Get-Content -Path $stdoutLog -Tail 40 | Out-String } else { "" }
$tailStderr = if (Test-Path $stderrLog) { Get-Content -Path $stderrLog -Tail 40 | Out-String } else { "" }

[ordered]@{
    CertificateThumbprint = $cert.Thumbprint
    ServiceStatus = [string]$finalService.Status
    ConfigPath = $ConfigPath
    PublishDirectory = $publishDir
    StdoutLogTail = $tailStdout.Trim()
    StderrLogTail = $tailStderr.Trim()
} | ConvertTo-Json -Depth 6
