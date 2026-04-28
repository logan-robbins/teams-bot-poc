# Teams Auto-Invite Setup

This project supports two meeting join modes:

- `invite_and_graph_join`
- `policy_auto_invite`

`policy_auto_invite` is the Teams policy-based automatic invitation path. Per current Microsoft Learn documentation, that path uses a Teams compliance recording application instance and a Teams compliance recording policy that invites the bot into calls and meetings for scoped users.

## Preconditions

- The Teams app manifest must declare `supportsCalling: true`.
- The bot app registration must have the required Graph application permissions and admin consent.
- The tenant admin must create a Teams online application instance for the bot app.
- The tenant admin must create a Teams compliance recording policy, associate the application instance, and assign the policy to the target users, group, or tenant scope.
- If you also use Microsoft Graph online meeting application-permission APIs, configure an application access policy separately.

## PowerShell Flow

Run from a machine with the `MicrosoftTeams` PowerShell module and Teams admin access:

```powershell
Connect-MicrosoftTeams

.\scripts\setup-policy-auto-invite.ps1 `
  -AppId "<bot-app-id>" `
  -ApplicationInstanceUpn "alfred-recorder@contoso.onmicrosoft.com" `
  -ApplicationInstanceDisplayName "Alfred Auto Invite" `
  -PolicyName "AlfredAutoInvitePolicy" `
  -RecordedUsers "organizer@contoso.com"
```

The script will:

1. Create or reuse a Teams online application instance.
2. Sync that instance into Agent Provisioning Service.
3. Create or reuse a Teams compliance recording policy.
4. Associate the application instance to that policy.
5. Grant the policy to the requested users, group, or globally.

## Runtime Config

Only enable policy mode in the bot config after tenant setup is complete:

```json
"JoinMode": {
  "PreferredMode": "policy_auto_invite",
  "PolicyAutoInviteEnabled": true,
  "AutoFallbackToInviteAndGraphJoin": true,
  "RequireBotAttendeeForInviteJoin": true
}
```

The VM bootstrap script now leaves policy mode disabled by default. To generate a production config that prefers auto-invite:

```powershell
pwsh ./scripts/bootstrap-production-vm.ps1 `
  -EnablePolicyAutoInvite 1
```

## Raw Microsoft Teams PowerShell Commands

These are the Microsoft Learn command shapes the script follows:

```powershell
New-CsOnlineApplicationInstance `
  -UserPrincipalName "cr.instance@contoso.onmicrosoft.com" `
  -DisplayName "ComplianceRecordingBotInstance" `
  -ApplicationId "<bot-app-id>"

Sync-CsOnlineApplicationInstance -ObjectId "<application-instance-object-id>"

New-CsTeamsComplianceRecordingPolicy `
  -Identity "AlfredAutoInvitePolicy" `
  -Enabled $true `
  -Description "Alfred policy-based auto-invite policy"

Set-CsTeamsComplianceRecordingPolicy `
  -Identity "AlfredAutoInvitePolicy" `
  -ComplianceRecordingApplications @(
    New-CsTeamsComplianceRecordingApplication `
      -Parent "AlfredAutoInvitePolicy" `
      -Id "<application-instance-object-id>" `
      -RequiredBeforeMeetingJoin $false `
      -RequiredDuringMeeting $false
  )

Grant-CsTeamsComplianceRecordingPolicy `
  -Identity "organizer@contoso.onmicrosoft.com" `
  -PolicyName "AlfredAutoInvitePolicy"
```

If your workflow also needs Graph application-permission access to online meetings, configure the separate application access policy:

```powershell
New-CsApplicationAccessPolicy `
  -Identity "AlfredOnlineMeetingsPolicy" `
  -AppIds "<bot-app-id>" `
  -Description "Allow Alfred to access online meetings"

Grant-CsApplicationAccessPolicy `
  -PolicyName "AlfredOnlineMeetingsPolicy" `
  -Identity "<organizer-object-id>"
```

## Operational Notes

- Auto-invite applies only to new calls and meetings after the compliance recording policy is assigned.
- `invite_and_graph_join` remains the default join mode unless you explicitly enable policy mode in bot config.
- Keep `AutoFallbackToInviteAndGraphJoin` enabled unless you want policy mode failures to block the workflow.
