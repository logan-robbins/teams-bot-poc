# Bootstrap a fresh Windows VM for the Teams media bot production deployment.
# Intended to run via: az vm run-command invoke --command-id RunPowerShellScript --scripts @bootstrap-production-vm.ps1

param(
    [string]$ProjectRoot = "C:\teams-bot-poc",
    [string]$RepoUrl = "https://github.com/logan-robbins/teams-bot-poc.git",
    [string]$RepoBranch = "main",
    [string]$ConfigPath = "C:\teams-bot-poc\src\Config\appsettings.production.json",
    [string]$AppId,
    [string]$AppSecret,
    [string]$TenantId,
    [string]$NotificationUrl,
    [string]$ServiceFqdn,
    [string]$InstancePublicIPAddress,
    [string]$CertificateThumbprint,
    [string]$TranscriptSinkPythonEndpoint,
    [string]$RunAsUser = "azureuser",
    [string]$RunAsPassword,
    [string]$SttProvider = "Deepgram",
    [string]$DeepgramApiKey,
    [string]$DeepgramModel = "nova-3",
    [bool]$DeepgramDiarize = $true,
    [string]$AzureSpeechKey,
    [string]$AzureSpeechRegion = "eastus",
    [string]$AzureSpeechRecognitionLanguage = "en-US",
    [int]$BotListenPort = 443,
    [int]$MediaPort = 8445,
    [int]$EnablePolicyAutoInvite = 0,
    [int]$AutoFallbackToInviteAndGraphJoin = 1,
    [int]$RequireBotAttendeeForInviteJoin = 1,
    [int]$BootstrapOnly = 0,
    [int]$SkipRepositorySync = 0
)

$ErrorActionPreference = "Stop"
$ChocolateyExe = "C:\ProgramData\chocolatey\bin\choco.exe"

function Assert-Required([string]$Name, [string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "Required parameter '$Name' was not provided."
    }
}

function Get-ChocolateyCommand {
    if (Get-Command choco -ErrorAction SilentlyContinue) {
        return "choco"
    }

    if (Test-Path $script:ChocolateyExe) {
        return $script:ChocolateyExe
    }

    throw "Chocolatey executable not found."
}

function Install-ChocolateyIfMissing {
    if ((Get-Command choco -ErrorAction SilentlyContinue) -or (Test-Path $script:ChocolateyExe)) {
        $env:Path = "C:\ProgramData\chocolatey\bin;$env:Path"
        return
    }

    Write-Host "Installing Chocolatey..." -ForegroundColor Yellow
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString("https://community.chocolatey.org/install.ps1"))
}

function Install-PackageIfMissing([string]$CommandName, [string]$PackageName) {
    if (Get-Command $CommandName -ErrorAction SilentlyContinue) {
        return
    }

    Write-Host "Installing $PackageName..." -ForegroundColor Yellow
    $choco = Get-ChocolateyCommand
    & $choco install $PackageName -y --no-progress
}

function Ensure-Repository([string]$Root, [string]$Url, [string]$Branch) {
    $gitDir = Join-Path $Root ".git"
    if (-not (Test-Path $gitDir)) {
        Write-Host "Cloning repository into $Root..." -ForegroundColor Yellow
        $parent = Split-Path -Parent $Root
        if (-not (Test-Path $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }

        git clone --branch $Branch --single-branch $Url $Root
        return
    }

    Write-Host "Updating repository in $Root..." -ForegroundColor Yellow
    Push-Location $Root
    try {
        git fetch origin $Branch
        git checkout $Branch
        git pull --ff-only origin $Branch
    }
    finally {
        Pop-Location
    }
}

function Write-ProductionConfig {
    param(
        [string]$Path,
        [string]$BotAppId,
        [string]$BotAppSecret,
        [string]$BotTenantId,
        [string]$BotNotificationUrl,
        [string]$BotServiceFqdn,
        [string]$PublicIp,
        [string]$CertThumbprint,
        [string]$SinkEndpoint,
        [string]$Provider,
        [string]$DeepgramKey,
        [string]$DeepgramSelectedModel,
        [bool]$EnableDiarization,
        [string]$SpeechKey,
        [string]$SpeechRegion,
        [string]$SpeechLanguage,
        [int]$ListenPort,
        [int]$PublicMediaPort,
        [bool]$PolicyAutoInviteEnabled,
        [bool]$AutoFallbackToInviteGraphJoin,
        [bool]$RequireBotInviteJoinAttendee
    )

    $providerName = if ([string]::IsNullOrWhiteSpace($Provider)) { "Deepgram" } else { $Provider.Trim() }
    $preferredJoinMode = if ($PolicyAutoInviteEnabled) { "policy_auto_invite" } else { "invite_and_graph_join" }

    $config = [ordered]@{
        Logging = @{
            LogLevel = @{
                Default = "Information"
                "Microsoft.AspNetCore" = "Warning"
                "Microsoft.Graph.Communications" = "Debug"
            }
        }
        AllowedHosts = "*"
        Bot = @{
            TenantId = $BotTenantId
            AppId = $BotAppId
            AppSecret = $BotAppSecret
            NotificationUrl = $BotNotificationUrl
            LocalHttpListenUrl = "https://0.0.0.0:$ListenPort"
            LocalHttpListenPort = $ListenPort
        }
        MediaPlatformSettings = @{
            ApplicationId = $BotAppId
            CertificateThumbprint = $CertThumbprint
            InstanceInternalPort = $PublicMediaPort
            InstancePublicPort = $PublicMediaPort
            ServiceFqdn = $BotServiceFqdn
            InstancePublicIPAddress = $PublicIp
        }
        Stt = @{
            Provider = $providerName
        }
        TranscriptSink = @{
            PythonEndpoint = $SinkEndpoint
        }
        JoinMode = @{
            PreferredMode = $preferredJoinMode
            PolicyAutoInviteEnabled = $PolicyAutoInviteEnabled
            AutoFallbackToInviteAndGraphJoin = $AutoFallbackToInviteGraphJoin
            RequireBotAttendeeForInviteJoin = $RequireBotInviteJoinAttendee
            TenantOverrides = @{
                $BotTenantId = @{
                    PreferredMode = $preferredJoinMode
                    PolicyAutoInviteEnabled = $PolicyAutoInviteEnabled
                    AutoFallbackToInviteAndGraphJoin = $AutoFallbackToInviteGraphJoin
                    RequireBotAttendeeForInviteJoin = $RequireBotInviteJoinAttendee
                }
            }
        }
    }

    if ($providerName -ieq "Deepgram") {
        Assert-Required "DeepgramApiKey" $DeepgramKey
        $config.Stt.Deepgram = @{
            ApiKey = $DeepgramKey
            Model = $DeepgramSelectedModel
            Diarize = $EnableDiarization
        }
    }
    elseif ($providerName -ieq "AzureSpeech") {
        Assert-Required "AzureSpeechKey" $SpeechKey
        Assert-Required "AzureSpeechRegion" $SpeechRegion
        $config.Stt.AzureSpeech = @{
            Key = $SpeechKey
            Region = $SpeechRegion
            RecognitionLanguage = $SpeechLanguage
            EndpointId = $null
        }
    }
    else {
        throw "Unsupported STT provider '$providerName'. Use Deepgram or AzureSpeech."
    }

    $configDir = Split-Path -Parent $Path
    if (-not (Test-Path $configDir)) {
        New-Item -ItemType Directory -Path $configDir -Force | Out-Null
    }

    $config | ConvertTo-Json -Depth 8 | Set-Content -Path $Path -Encoding UTF8
}

function Ensure-Service {
    param(
        [string]$ExePath,
        [string]$WorkingDirectory,
        [string]$ConfigFile,
        [string]$ServiceUser,
        [string]$ServicePassword,
        [string]$StdoutPath,
        [string]$StderrPath
    )

    $service = Get-Service -Name "TeamsMediaBot" -ErrorAction SilentlyContinue
    if (-not $service) {
        Write-Host "Installing TeamsMediaBot service..." -ForegroundColor Yellow
        nssm install TeamsMediaBot $ExePath "--config $ConfigFile"
    }
    else {
        Write-Host "Updating TeamsMediaBot service configuration..." -ForegroundColor Yellow
        try {
            Stop-Service TeamsMediaBot -Force -ErrorAction SilentlyContinue
        }
        catch {
        }
    }

    nssm set TeamsMediaBot Application $ExePath
    nssm set TeamsMediaBot AppDirectory $WorkingDirectory
    nssm set TeamsMediaBot AppParameters "--config $ConfigFile"
    nssm set TeamsMediaBot Start SERVICE_AUTO_START
    nssm set TeamsMediaBot AppStdout $StdoutPath
    nssm set TeamsMediaBot AppStderr $StderrPath

    if (-not [string]::IsNullOrWhiteSpace($ServiceUser) -and -not [string]::IsNullOrWhiteSpace($ServicePassword)) {
        nssm set TeamsMediaBot ObjectName ".\$ServiceUser" $ServicePassword
    }
}

Assert-Required "AppId" $AppId
Assert-Required "AppSecret" $AppSecret
Assert-Required "TenantId" $TenantId
Assert-Required "NotificationUrl" $NotificationUrl
Assert-Required "ServiceFqdn" $ServiceFqdn
Assert-Required "InstancePublicIPAddress" $InstancePublicIPAddress
Assert-Required "TranscriptSinkPythonEndpoint" $TranscriptSinkPythonEndpoint

if (($BootstrapOnly -eq 1) -and [string]::IsNullOrWhiteSpace($CertificateThumbprint)) {
    $CertificateThumbprint = "CHANGE_AFTER_CERT_INSTALL"
}
else {
    Assert-Required "CertificateThumbprint" $CertificateThumbprint
}

Write-Host "Preparing Windows VM for production deployment..." -ForegroundColor Cyan

Install-ChocolateyIfMissing
Install-PackageIfMissing "git" "git"
Install-PackageIfMissing "dotnet" "dotnet-8.0-sdk"
Install-PackageIfMissing "nssm" "nssm"

$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
    [System.Environment]::GetEnvironmentVariable("Path", "User")

if ($SkipRepositorySync -ne 1) {
    Ensure-Repository -Root $ProjectRoot -Url $RepoUrl -Branch $RepoBranch
}
elseif (-not (Test-Path (Join-Path $ProjectRoot ".git"))) {
    throw "SkipRepositorySync=1 was requested, but no git repository exists at '$ProjectRoot'."
}

$logsDir = Join-Path $ProjectRoot "logs"
$publishDir = Join-Path $ProjectRoot "src\bin\Release\net8.0\publish"
$stdoutLog = Join-Path $logsDir "service-output.log"
$stderrLog = Join-Path $logsDir "service-error.log"
if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
}

Write-ProductionConfig `
    -Path $ConfigPath `
    -BotAppId $AppId `
    -BotAppSecret $AppSecret `
    -BotTenantId $TenantId `
    -BotNotificationUrl $NotificationUrl `
    -BotServiceFqdn $ServiceFqdn `
    -PublicIp $InstancePublicIPAddress `
    -CertThumbprint $CertificateThumbprint `
    -SinkEndpoint $TranscriptSinkPythonEndpoint `
    -Provider $SttProvider `
    -DeepgramKey $DeepgramApiKey `
    -DeepgramSelectedModel $DeepgramModel `
    -EnableDiarization $DeepgramDiarize `
    -SpeechKey $AzureSpeechKey `
    -SpeechRegion $AzureSpeechRegion `
    -SpeechLanguage $AzureSpeechRecognitionLanguage `
    -ListenPort $BotListenPort `
    -PublicMediaPort $MediaPort `
    -PolicyAutoInviteEnabled ($EnablePolicyAutoInvite -ne 0) `
    -AutoFallbackToInviteGraphJoin ($AutoFallbackToInviteAndGraphJoin -ne 0) `
    -RequireBotInviteJoinAttendee ($RequireBotAttendeeForInviteJoin -ne 0)

Push-Location (Join-Path $ProjectRoot "src")
try {
    dotnet restore
    dotnet publish --configuration Release --output $publishDir
}
finally {
    Pop-Location
}

$exePath = Join-Path $publishDir "TeamsMediaBot.exe"
if (-not (Test-Path $exePath)) {
    throw "Published executable not found at '$exePath'."
}

$serviceStatus = "NotInstalled"
if ($BootstrapOnly -ne 1) {
    Ensure-Service `
        -ExePath $exePath `
        -WorkingDirectory $publishDir `
        -ConfigFile $ConfigPath `
        -ServiceUser $RunAsUser `
        -ServicePassword $RunAsPassword `
        -StdoutPath $stdoutLog `
        -StderrPath $stderrLog

    Start-Service TeamsMediaBot
    Start-Sleep -Seconds 5
    $serviceStatus = (Get-Service TeamsMediaBot).Status
}

$health = @{
    ServiceStatus = $serviceStatus
    ConfigPath = $ConfigPath
    PublishDirectory = $publishDir
    StdoutLog = $stdoutLog
    StderrLog = $stderrLog
    BootstrapOnly = ($BootstrapOnly -eq 1)
}

$health | ConvertTo-Json
