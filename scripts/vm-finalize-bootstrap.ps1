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

$bootstrapScript = Join-Path $ProjectRoot "scripts\bootstrap-production-vm.ps1"
if (-not (Test-Path $bootstrapScript)) {
    throw "Bootstrap script not found at '$bootstrapScript'."
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
    throw "No bot certificate was found in Cert:\\LocalMachine\\My."
}

$sttProvider = if ([string]::IsNullOrWhiteSpace($config.Stt.Provider)) { "Deepgram" } else { [string]$config.Stt.Provider }
$botListenPort = if ($config.Bot.LocalHttpListenPort) { [int]$config.Bot.LocalHttpListenPort } else { 443 }
$mediaPort = if ($config.MediaPlatformSettings.InstancePublicPort) { [int]$config.MediaPlatformSettings.InstancePublicPort } else { 8445 }

$params = @{
    ProjectRoot = $ProjectRoot
    ConfigPath = $ConfigPath
    AppId = [string]$config.Bot.AppId
    AppSecret = [string]$config.Bot.AppSecret
    TenantId = [string]$config.Bot.TenantId
    NotificationUrl = [string]$config.Bot.NotificationUrl
    ServiceFqdn = [string]$config.MediaPlatformSettings.ServiceFqdn
    InstancePublicIPAddress = [string]$config.MediaPlatformSettings.InstancePublicIPAddress
    CertificateThumbprint = [string]$cert.Thumbprint
    TranscriptSinkPythonEndpoint = [string]$config.TranscriptSink.PythonEndpoint
    RunAsUser = $RunAsUser
    RunAsPassword = $RunAsPassword
    SttProvider = $sttProvider
    BotListenPort = $botListenPort
    MediaPort = $mediaPort
    SkipRepositorySync = 1
    BootstrapOnly = 0
}

if ($sttProvider -ieq "AzureSpeech" -and $null -ne $config.Stt.AzureSpeech) {
    $params.AzureSpeechKey = [string]$config.Stt.AzureSpeech.Key
    $params.AzureSpeechRegion = [string]$config.Stt.AzureSpeech.Region
    $params.AzureSpeechRecognitionLanguage = [string]$config.Stt.AzureSpeech.RecognitionLanguage
}
elseif ($sttProvider -ieq "Deepgram" -and $null -ne $config.Stt.Deepgram) {
    $params.DeepgramApiKey = [string]$config.Stt.Deepgram.ApiKey
    $params.DeepgramModel = [string]$config.Stt.Deepgram.Model
    if ($null -ne $config.Stt.Deepgram.Diarize) {
        $params.DeepgramDiarize = [bool]$config.Stt.Deepgram.Diarize
    }
}

$bootstrapResult = & $bootstrapScript @params | Out-String

$service = Get-Service -Name "TeamsMediaBot" -ErrorAction SilentlyContinue

[ordered]@{
    CertificateSubject = $cert.Subject
    CertificateThumbprint = $cert.Thumbprint
    CertificateNotAfter = $cert.NotAfter
    ServiceStatus = if ($service) { [string]$service.Status } else { "NotInstalled" }
    BootstrapResult = $bootstrapResult.Trim()
} | ConvertTo-Json -Depth 6
