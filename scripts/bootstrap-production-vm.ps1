# Bootstrap a fresh Windows VM for the Teams media bot production deployment.
# Intended to run via: az vm run-command invoke --command-id RunPowerShellScript --scripts @bootstrap-production-vm.ps1

param(
    [string]$ProjectRoot = "C:\teams-bot-poc",
    [string]$RepoUrl = "git@github.com:logan-robbins/alfred-teams-bot.git",
    [string]$RepoBranch = "main",
    [string]$DeployKey = "",
    [string]$DeployKeyPath = "C:\ProgramData\alfred\deploy_key",
    [string]$ConfigPath = "C:\teams-bot-poc\src\Config\appsettings.production.json",
    [string]$AppId,
    [string]$AppSecret,
    [string]$TenantId,
    [string]$NotificationUrl,
    [string]$ServiceFqdn,
    [string]$InstancePublicIPAddress,
    [string]$CertificateThumbprint,
    [string]$TranscriptSinkPythonEndpoint,
    [string]$TranscriptSinkChatEndpoint,
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
    $installerPath = Join-Path $env:TEMP "install-chocolatey.ps1"
    try {
        Invoke-WebRequest `
            -Uri "https://community.chocolatey.org/install.ps1" `
            -OutFile $installerPath `
            -UseBasicParsing `
            -TimeoutSec 180
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installerPath
    }
    finally {
        Remove-Item $installerPath -Force -ErrorAction SilentlyContinue
    }

    $env:Path = "C:\ProgramData\chocolatey\bin;$env:Path"
}

function Install-PackageIfMissing([string]$CommandName, [string]$PackageName) {
    if (Get-Command $CommandName -ErrorAction SilentlyContinue) {
        return
    }

    Write-Host "Installing $PackageName..." -ForegroundColor Yellow
    $choco = Get-ChocolateyCommand
    & $choco install $PackageName -y --no-progress --execution-timeout=600
}

function Install-DotnetSdkIfMissing {
    # Get-Command dotnet returning true is not sufficient — Windows Server
    # ships dotnet runtime/framework binaries that can't run `restore`/`publish`.
    # Verify a real SDK is present via `dotnet --list-sdks`.
    $sdksPresent = $false
    if (Get-Command dotnet -ErrorAction SilentlyContinue) {
        try {
            $sdks = & dotnet --list-sdks 2>$null
            if ($sdks -and ($sdks | Where-Object { $_ -match '^\d' }).Count -gt 0) {
                $sdksPresent = $true
            }
        } catch {
            $sdksPresent = $false
        }
    }
    if ($sdksPresent) { return }

    Write-Host "Installing .NET 8 SDK via Microsoft's official dotnet-install.ps1..." -ForegroundColor Yellow

    $installerPath = Join-Path $env:TEMP "dotnet-install.ps1"
    $installRoot = "C:\Program Files\dotnet"
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072

    Invoke-WebRequest `
        -Uri "https://dot.net/v1/dotnet-install.ps1" `
        -OutFile $installerPath `
        -UseBasicParsing `
        -TimeoutSec 180

    & $installerPath -Channel 8.0 -Quality GA -InstallDir $installRoot

    # Persist the new dotnet on the machine PATH for the service later.
    $machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    if ($machinePath -notlike "*$installRoot*") {
        [System.Environment]::SetEnvironmentVariable("Path", "$machinePath;$installRoot", "Machine")
    }
    # And the current process PATH so the same script can run dotnet right away.
    $env:Path = "$installRoot;" + $env:Path

    # Verify
    $sdks = & dotnet --list-sdks 2>$null
    if (-not $sdks -or ($sdks | Where-Object { $_ -match '^8\.' }).Count -eq 0) {
        throw ".NET 8 SDK install completed but `dotnet --list-sdks` does not show an 8.x entry."
    }
}

function Install-MediaPlatformPrereqs {
    # Required by Microsoft.Graph.Communications.Calls.Media (NativeMedia.dll).
    # Without these the bot crashloops with: DllNotFoundException: Unable to
    # load DLL 'NativeMedia' or one of its dependencies.
    $smf = Get-WindowsFeature -Name Server-Media-Foundation -ErrorAction SilentlyContinue
    if ($smf -and -not $smf.Installed) {
        Write-Host "Installing Server-Media-Foundation Windows feature..." -ForegroundColor Yellow
        Install-WindowsFeature -Name Server-Media-Foundation | Out-Null
    }

    if (-not (Test-Path 'C:\Windows\System32\msvcp140.dll')) {
        Write-Host "Installing Visual C++ 2015-2022 Redistributable (vcredist140)..." -ForegroundColor Yellow
        $choco = Get-ChocolateyCommand
        & $choco install vcredist140 -y --no-progress --execution-timeout=600
    }
}

function Install-OpenSSHServerIfMissing {
    # Installs sshd so future recovery does not require RDP. Companion NSG
    # rules for ports 22 and 5986 are added in scripts/deploy-azure-vm.sh.
    $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' -ErrorAction SilentlyContinue
    if (-not $cap -or $cap.State -ne 'Installed') {
        Write-Host "Installing OpenSSH Server capability..." -ForegroundColor Yellow
        Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
    }

    $sshd = Get-Service sshd -ErrorAction SilentlyContinue
    if (-not $sshd) {
        Write-Warning "OpenSSH Server installed but sshd service not found; skipping startup config."
        return
    }
    if ($sshd.StartType -ne 'Automatic') {
        Set-Service -Name sshd -StartupType Automatic
    }
    if ($sshd.Status -ne 'Running') {
        Start-Service sshd
    }

    if (-not (Get-NetFirewallRule -Name 'sshd-22' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'sshd-22' -DisplayName 'OpenSSH Server (sshd)' `
            -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    }
    if (-not (Get-NetFirewallRule -Name 'WinRM-HTTPS-5986' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'WinRM-HTTPS-5986' -DisplayName 'WinRM HTTPS' `
            -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 5986 | Out-Null
    }
}

function Setup-DeployKey {
    param(
        [string]$KeyContent,
        [string]$KeyPath,
        [string]$ServiceUser
    )

    if ([string]::IsNullOrWhiteSpace($KeyContent)) {
        return  # No key supplied — caller is using a public repo or pre-configured auth.
    }

    $parent = Split-Path -Parent $KeyPath
    if (-not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    # Normalize CRLF → LF (PowerShell here-strings can introduce CRLF; OpenSSH refuses).
    $normalized = ($KeyContent -replace "`r`n", "`n").TrimEnd() + "`n"

    # On re-runs the file was previously locked down to SYSTEM:R + ServiceUser:R
    # by the icacls block below, which prevents WriteAllText from overwriting it
    # (UnauthorizedAccessException). Restore SYSTEM full control and clear the
    # read-only attribute before writing; the strict ACL is reapplied immediately
    # afterwards.
    if (Test-Path $KeyPath) {
        icacls $KeyPath /inheritance:r 2>&1 | Out-Null
        icacls $KeyPath /grant:r "NT AUTHORITY\SYSTEM:F" "Administrators:F" 2>&1 | Out-Null
        Set-ItemProperty -Path $KeyPath -Name IsReadOnly -Value $false -ErrorAction SilentlyContinue
    }
    [System.IO.File]::WriteAllText($KeyPath, $normalized, [System.Text.UTF8Encoding]::new($false))

    # Lock the file down so OpenSSH stops complaining about loose permissions.
    icacls $KeyPath /inheritance:r 2>&1 | Out-Null
    icacls $KeyPath /grant:r "NT AUTHORITY\SYSTEM:R" 2>&1 | Out-Null
    if (-not [string]::IsNullOrWhiteSpace($ServiceUser)) {
        icacls $KeyPath /grant:r "${ServiceUser}:R" 2>&1 | Out-Null
    }

    # Tell git to use this key + auto-accept github.com's host key on first connect.
    $env:GIT_SSH_COMMAND = "ssh -i `"$KeyPath`" -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"
    Write-Host "Configured GIT_SSH_COMMAND to use deploy key at $KeyPath" -ForegroundColor Yellow
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
        # Migrate existing checkouts whose origin still points at an older URL
        # (e.g. when this VM was bootstrapped against the public repo and the
        # working branch has since moved to a private repo).
        $currentUrl = (git config --get remote.origin.url 2>$null)
        if ($currentUrl -and $currentUrl -ne $Url) {
            Write-Host "Updating remote origin URL: $currentUrl -> $Url" -ForegroundColor Yellow
            git remote set-url origin $Url
        }
        # Original clone used --single-branch which leaves a refspec like
        # `+refs/heads/feat/alfred-chat-modality:refs/remotes/origin/feat/...`,
        # so a later `git fetch origin main` returns 0 but doesn't update
        # refs/remotes/origin/main. Normalize to the all-branches refspec.
        $currentFetch = (git config --get remote.origin.fetch 2>$null)
        $allBranchesRefspec = "+refs/heads/*:refs/remotes/origin/*"
        if ($currentFetch -ne $allBranchesRefspec) {
            Write-Host "Normalizing remote.origin.fetch: $currentFetch -> $allBranchesRefspec" -ForegroundColor Yellow
            git config remote.origin.fetch $allBranchesRefspec
        }
        git fetch --prune origin
        # Force the checkout to track origin/$Branch even if a local branch of
        # the same name had been pointing somewhere else (covers branch renames).
        git checkout -B $Branch "origin/$Branch"
        git reset --hard "origin/$Branch"
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
        [string]$SinkChatEndpoint,
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

    # Cert thumbprint resolution order:
    #   1. caller-passed -CertThumbprint
    #   2. live cert in LocalMachine\My matching CN=$BotServiceFqdn or
    #      FriendlyName starts with "alfred-disney-cert" / "alfred-bot-cert"
    #   3. placeholder (the bot resolves the live cert by Subject/FriendlyName
    #      at runtime, so the placeholder is also tolerated)
    $effectiveCertThumbprint = $CertThumbprint
    $effectiveFriendlyPrefix = "alfred-disney-cert"
    if ([string]::IsNullOrWhiteSpace($effectiveCertThumbprint)) {
        try {
            $liveCert = Get-ChildItem Cert:\LocalMachine\My -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.Subject -match ("CN=" + [regex]::Escape($BotServiceFqdn)) -or
                    $_.FriendlyName -like "alfred-disney-cert*" -or
                    $_.FriendlyName -like "alfred-bot-cert*"
                } |
                Sort-Object NotAfter -Descending |
                Select-Object -First 1
            if ($liveCert) {
                $effectiveCertThumbprint = $liveCert.Thumbprint
                if (-not [string]::IsNullOrWhiteSpace($liveCert.FriendlyName)) {
                    $effectiveFriendlyPrefix = ($liveCert.FriendlyName -split ' @ ')[0].Trim()
                }
                Write-Host ("Resolved live cert from store (Thumbprint=" + $liveCert.Thumbprint + ", FriendlyName='" + $liveCert.FriendlyName + "').") -ForegroundColor Yellow
            }
        }
        catch {
            Write-Warning "Could not enumerate LocalMachine\My cert store: $($_.Exception.Message)"
        }
    }
    if ([string]::IsNullOrWhiteSpace($effectiveCertThumbprint)) {
        $effectiveCertThumbprint = "CHANGE_AFTER_CERT_INSTALL"
    }

    $config = [ordered]@{
        Logging = @{
            LogLevel = @{
                Default = "Information"
                "Microsoft.AspNetCore" = "Warning"
                "Microsoft.Graph.Communications" = "Debug"
            }
        }
        AllowedHosts = "*"
        # Bot Framework's ConfigurationBotFrameworkAuthentication (used by
        # CloudAdapter for /api/messages) reads these root-level keys, distinct
        # from the Bot.* keys the Graph Communications media SDK consumes below.
        # Without these, /api/messages returns 401 "Invalid AppId passed on token".
        MicrosoftAppType = "SingleTenant"
        MicrosoftAppId = $BotAppId
        MicrosoftAppPassword = $BotAppSecret
        MicrosoftAppTenantId = $BotTenantId
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
            CertificateThumbprint = $effectiveCertThumbprint
            CertificateFriendlyName = $effectiveFriendlyPrefix
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
            ChatEndpoint = if ([string]::IsNullOrWhiteSpace($SinkChatEndpoint)) { ($SinkEndpoint -replace '/transcript$', '/chat') } else { $SinkChatEndpoint }
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

if (($BootstrapOnly -ne 1) -or -not [string]::IsNullOrWhiteSpace($CertificateThumbprint)) {
    Assert-Required "CertificateThumbprint" $CertificateThumbprint
}

Write-Host "Preparing Windows VM for production deployment..." -ForegroundColor Cyan

Install-ChocolateyIfMissing
Install-PackageIfMissing "git" "git"
Install-DotnetSdkIfMissing
Install-PackageIfMissing "nssm" "nssm"
Install-MediaPlatformPrereqs
Install-OpenSSHServerIfMissing

$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
    [System.Environment]::GetEnvironmentVariable("Path", "User")

# Configure SSH deploy key (if supplied) so private-repo clones work without PAT.
Setup-DeployKey -KeyContent $DeployKey -KeyPath $DeployKeyPath -ServiceUser $RunAsUser

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
    -SinkChatEndpoint $TranscriptSinkChatEndpoint `
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

$existingService = Get-Service -Name "TeamsMediaBot" -ErrorAction SilentlyContinue
$wasServiceRunning = $false
if ($existingService -and $existingService.Status -ne "Stopped") {
    $wasServiceRunning = $true
    Write-Host "Stopping TeamsMediaBot before publish so DLLs are not locked..." -ForegroundColor Yellow
    Stop-Service TeamsMediaBot -Force -ErrorAction Stop
    $existingService.WaitForStatus("Stopped", "00:00:30")
}

Push-Location (Join-Path $ProjectRoot "src")
try {
    dotnet restore
    dotnet publish --configuration Release --output $publishDir
}
finally {
    Pop-Location
}

if (($BootstrapOnly -eq 1) -and $wasServiceRunning) {
    Write-Host "Restarting TeamsMediaBot after publish." -ForegroundColor Yellow
    Start-Service TeamsMediaBot
    Start-Sleep -Seconds 5
}

$exePath = Join-Path $publishDir "TeamsMediaBot.exe"
if (-not (Test-Path $exePath)) {
    throw "Published executable not found at '$exePath'."
}

$serviceStatus = if (Get-Service -Name "TeamsMediaBot" -ErrorAction SilentlyContinue) {
    [string](Get-Service -Name "TeamsMediaBot").Status
}
else {
    "NotInstalled"
}
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
