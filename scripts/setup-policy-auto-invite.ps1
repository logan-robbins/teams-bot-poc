[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$AppId,

    [Parameter(Mandatory = $true)]
    [string]$ApplicationInstanceUpn,

    [Parameter(Mandatory = $true)]
    [string]$PolicyName,

    [string]$ApplicationInstanceDisplayName = "Talestral Auto Invite",
    [string[]]$RecordedUsers = @(),
    [string]$RecordedGroup = "",
    [switch]$Global,
    [bool]$RequiredBeforeMeetingJoin = $false,
    [bool]$RequiredDuringMeeting = $false,
    [bool]$RequiredBeforeCallEstablishment = $false,
    [bool]$RequiredDuringCall = $false,
    [uint32]$ConcurrentInvitationCount = 1,
    [int]$Priority = 10,
    [switch]$SkipConnect
)

$ErrorActionPreference = "Stop"

function Assert-Cmd([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found. Install/import the MicrosoftTeams PowerShell module first."
    }
}

function Ensure-TeamsConnection {
    if ($SkipConnect) {
        return
    }

    try {
        Get-CsTenant -ErrorAction Stop | Out-Null
    }
    catch {
        Write-Host "Connecting to Microsoft Teams PowerShell..." -ForegroundColor Yellow
        Connect-MicrosoftTeams -ErrorAction Stop | Out-Null
    }
}

function Get-OrCreateApplicationInstance {
    param(
        [string]$UserPrincipalName,
        [string]$DisplayName,
        [string]$ApplicationId
    )

    $existing = Get-CsOnlineApplicationInstance -Identity $UserPrincipalName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Using existing application instance: $UserPrincipalName" -ForegroundColor Green
        return $existing
    }

    if (-not $PSCmdlet.ShouldProcess($UserPrincipalName, "Create Teams online application instance")) {
        return $null
    }

    Write-Host "Creating application instance: $UserPrincipalName" -ForegroundColor Yellow
    $created = New-CsOnlineApplicationInstance `
        -UserPrincipalName $UserPrincipalName `
        -DisplayName $DisplayName `
        -ApplicationId $ApplicationId

    Start-Sleep -Seconds 5
    $objectId = if ($created.ObjectId) { $created.ObjectId } else { $created.Id }
    if ($objectId) {
        Write-Host "Syncing application instance into Agent Provisioning Service..." -ForegroundColor Yellow
        Sync-CsOnlineApplicationInstance -ObjectId $objectId -ApplicationId $ApplicationId | Out-Null
    }

    return Get-CsOnlineApplicationInstance -Identity $UserPrincipalName -ErrorAction Stop
}

function Ensure-CompliancePolicy {
    param([string]$Identity)

    $policy = Get-CsTeamsComplianceRecordingPolicy -Identity $Identity -ErrorAction SilentlyContinue
    if ($policy) {
        Write-Host "Using existing compliance recording policy: $Identity" -ForegroundColor Green
        return $policy
    }

    if (-not $PSCmdlet.ShouldProcess($Identity, "Create Teams compliance recording policy")) {
        return $null
    }

    Write-Host "Creating compliance recording policy: $Identity" -ForegroundColor Yellow
    return New-CsTeamsComplianceRecordingPolicy `
        -Identity $Identity `
        -Enabled $true `
        -Description "Talestral policy-based auto-invite policy"
}

function Set-PolicyApplication {
    param(
        [string]$Identity,
        [string]$ObjectId
    )

    $recordingApplication = New-CsTeamsComplianceRecordingApplication `
        -Parent $Identity `
        -Id $ObjectId `
        -RequiredBeforeMeetingJoin $RequiredBeforeMeetingJoin `
        -RequiredDuringMeeting $RequiredDuringMeeting `
        -RequiredBeforeCallEstablishment $RequiredBeforeCallEstablishment `
        -RequiredDuringCall $RequiredDuringCall `
        -ConcurrentInvitationCount $ConcurrentInvitationCount `
        -Priority $Priority

    if (-not $PSCmdlet.ShouldProcess($Identity, "Associate compliance recording application instance")) {
        return
    }

    Write-Host "Associating application instance $ObjectId with policy $Identity" -ForegroundColor Yellow
    Set-CsTeamsComplianceRecordingPolicy `
        -Identity $Identity `
        -Enabled $true `
        -ComplianceRecordingApplications @($recordingApplication) | Out-Null
}

function Grant-PolicyAssignments {
    param([string]$Identity)

    if ($Global) {
        if ($PSCmdlet.ShouldProcess("Tenant", "Grant compliance recording policy $Identity globally")) {
            Write-Host "Granting policy globally..." -ForegroundColor Yellow
            Grant-CsTeamsComplianceRecordingPolicy -Global -PolicyName $Identity | Out-Null
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($RecordedGroup)) {
        if ($PSCmdlet.ShouldProcess($RecordedGroup, "Grant compliance recording policy $Identity to group")) {
            Write-Host "Granting policy to group: $RecordedGroup" -ForegroundColor Yellow
            Grant-CsTeamsComplianceRecordingPolicy -Group $RecordedGroup -PolicyName $Identity | Out-Null
        }
    }

    foreach ($user in $RecordedUsers) {
        if ([string]::IsNullOrWhiteSpace($user)) {
            continue
        }

        if ($PSCmdlet.ShouldProcess($user, "Grant compliance recording policy $Identity to user")) {
            Write-Host "Granting policy to user: $user" -ForegroundColor Yellow
            Grant-CsTeamsComplianceRecordingPolicy -Identity $user -PolicyName $Identity | Out-Null
        }
    }
}

Import-Module MicrosoftTeams -ErrorAction Stop

Assert-Cmd "Get-CsOnlineApplicationInstance"
Assert-Cmd "New-CsOnlineApplicationInstance"
Assert-Cmd "Sync-CsOnlineApplicationInstance"
Assert-Cmd "Get-CsTeamsComplianceRecordingPolicy"
Assert-Cmd "New-CsTeamsComplianceRecordingPolicy"
Assert-Cmd "New-CsTeamsComplianceRecordingApplication"
Assert-Cmd "Set-CsTeamsComplianceRecordingPolicy"
Assert-Cmd "Grant-CsTeamsComplianceRecordingPolicy"

Ensure-TeamsConnection

$instance = Get-OrCreateApplicationInstance `
    -UserPrincipalName $ApplicationInstanceUpn `
    -DisplayName $ApplicationInstanceDisplayName `
    -ApplicationId $AppId

if (-not $instance) {
    Write-Host "Application instance creation skipped by WhatIf/Confirm." -ForegroundColor Yellow
    return
}

$policy = Ensure-CompliancePolicy -Identity $PolicyName
if (-not $policy) {
    Write-Host "Policy creation skipped by WhatIf/Confirm." -ForegroundColor Yellow
    return
}

$objectId = if ($instance.ObjectId) { $instance.ObjectId } else { $instance.Id }
Set-PolicyApplication -Identity $PolicyName -ObjectId $objectId
Grant-PolicyAssignments -Identity $PolicyName

Write-Host ""
Write-Host "Auto-invite setup complete." -ForegroundColor Green
Write-Host "Application instance UPN: $ApplicationInstanceUpn"
Write-Host "Application instance ObjectId: $objectId"
Write-Host "Compliance policy: $PolicyName"
if ($RecordedUsers.Count -gt 0) {
    Write-Host "Assigned users: $($RecordedUsers -join ', ')"
}
if (-not [string]::IsNullOrWhiteSpace($RecordedGroup)) {
    Write-Host "Assigned group: $RecordedGroup"
}
if ($Global) {
    Write-Host "Assigned globally: true"
}
