# Install the Alfred Teams app into a specific meeting's chat thread.
#
# Per 2026 Microsoft Learn guidance, meeting-chat RSC permissions
# (ChatMessage.Read.Chat, etc.) only activate after the Teams app is
# installed in the meeting chat. Compliance/policy-based recording does
# NOT auto-install; this script does it programmatically.
#
# Graph application permission required:
#   TeamsAppInstallation.ReadWriteForChat.All  (admin consent)
#
# Usage:
#   pwsh ./install-bot-in-chat.ps1 `
#     -TenantId <tenant-guid> `
#     -AppId <entra-app-id> `
#     -AppSecret <secret> `
#     -TeamsAppId <teams-app-catalog-id> `
#     -OrganizerUpn <organizer@domain> `
#     -MeetingJoinUrl 'https://teams.microsoft.com/l/meetup-join/...'
#
# The script:
#   1. Acquires an app token for Microsoft Graph.
#   2. Resolves chatInfo.threadId from the meeting join URL via
#      /users/{upn}/onlineMeetings/getByJoinWebUrl.
#   3. POSTs to /chats/{threadId}/installedApps with the Teams app binding.

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$TenantId,
    [Parameter(Mandatory = $true)][string]$AppId,
    [Parameter(Mandatory = $true)][string]$AppSecret,
    [Parameter(Mandatory = $true)][string]$TeamsAppId,
    [Parameter(Mandatory = $true)][string]$OrganizerUpn,
    [Parameter(Mandatory = $true)][string]$MeetingJoinUrl
)

$ErrorActionPreference = "Stop"

function Get-GraphToken {
    param([string]$TenantId, [string]$AppId, [string]$AppSecret)
    $body = @{
        grant_type    = "client_credentials"
        client_id     = $AppId
        client_secret = $AppSecret
        scope         = "https://graph.microsoft.com/.default"
    }
    $resp = Invoke-RestMethod `
        -Method POST `
        -Uri "https://login.microsoftonline.com/$TenantId/oauth2/v2.0/token" `
        -ContentType "application/x-www-form-urlencoded" `
        -Body $body
    return $resp.access_token
}

function Resolve-MeetingChatThreadId {
    param([string]$Token, [string]$OrganizerUpn, [string]$JoinUrl)
    # Graph requires the joinWebUrl to be percent-encoded inside the
    # getByJoinWebUrl(...) function call.
    $escaped = [System.Uri]::EscapeDataString($JoinUrl)
    $uri = "https://graph.microsoft.com/v1.0/users/$OrganizerUpn/onlineMeetings/getByJoinWebUrl(joinWebUrl='$escaped')"
    $resp = Invoke-RestMethod -Method GET -Uri $uri -Headers @{ Authorization = "Bearer $Token" }
    if (-not $resp.chatInfo.threadId) {
        throw "Resolved online meeting has no chatInfo.threadId. Meeting may be a 1:1 call or have private-chat disabled."
    }
    return $resp.chatInfo.threadId
}

function Install-TeamsAppInChat {
    param(
        [string]$Token,
        [string]$ChatId,
        [string]$TeamsAppId
    )
    $uri = "https://graph.microsoft.com/v1.0/chats/$ChatId/installedApps"
    $body = @{
        "teamsApp@odata.bind" = "https://graph.microsoft.com/v1.0/appCatalogs/teamsApps/$TeamsAppId"
    } | ConvertTo-Json
    Invoke-RestMethod `
        -Method POST `
        -Uri $uri `
        -Headers @{ Authorization = "Bearer $Token"; "Content-Type" = "application/json" } `
        -Body $body
}

Write-Host "Acquiring Graph app token..."
$token = Get-GraphToken -TenantId $TenantId -AppId $AppId -AppSecret $AppSecret

Write-Host "Resolving meeting chat thread id for $OrganizerUpn..."
$chatId = Resolve-MeetingChatThreadId -Token $token -OrganizerUpn $OrganizerUpn -JoinUrl $MeetingJoinUrl
Write-Host "chatId = $chatId"

if ($PSCmdlet.ShouldProcess("chat:$chatId", "Install Teams app $TeamsAppId")) {
    Write-Host "Installing app $TeamsAppId into chat..."
    try {
        Install-TeamsAppInChat -Token $token -ChatId $chatId -TeamsAppId $TeamsAppId
        Write-Host "OK: Alfred installed in meeting chat. RSC permissions are now active."
    } catch {
        $resp = $_.Exception.Response
        if ($resp -and $resp.StatusCode.value__ -eq 409) {
            Write-Host "Already installed (HTTP 409). No-op."
        } else {
            throw
        }
    }
}
