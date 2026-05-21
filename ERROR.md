# ERROR — Bot call join blocked by `7504/7505`

## 2026-05-21 investigation update

The previous conclusion that this is only delayed
`CsApplicationAccessPolicy` propagation is incomplete.

Current evidence:

- The deployed bot is healthy and Azure Bot Service has Teams calling
  enabled with `incomingCallRoute=graphPma`.
- The failing request reaches Microsoft Graph `/communications/calls`
  through `Calls.AddAsync` and Graph returns `403` with:
  `Insufficient enterprise tenant permissions, cannot access this API.`
- The Sandbox Entra app registration for
  `207a38a4-67c5-4ef9-ada8-ea7998734d59` has
  `requiredResourceAccess: []` and `signInAudience: AzureADMultipleOrgs`.
  That means there are no tenant-wide Graph `Calls.*` application
  permissions granted to the app; Alfred can only rely on per-meeting /
  per-chat RSC grants from Teams app installs.
- The repo is on the latest stable Graph Communications SDK package
  family (`1.2.0.15690`); NuGet also lists `1.2.0-beta.16019`.
  The immediate failure is a Graph authorization rejection, not a media
  socket or SDK load failure.
- Microsoft 2026-facing guidance and Q&A for this exact 7504 payload
  describe a tenant-level Cloud Communications calling / application-
  hosted-media enablement gate. `CsApplicationAccessPolicy` may be one
  admin prerequisite for tenant-wide online-meeting access, but waiting
  for policy propagation alone is not sufficient evidence that call
  joining should start working.

Correct working theory: the failing manual join is trying to use a
calling/media capability that is not authorized for this tenant/app
combination. The two viable paths are:

1. Keep the RSC-only design and test only meetings where Alfred has
   actually been added through `+ Apps` so `Calls.JoinGroupCalls.Chat`
   and `Calls.AccessMedia.Chat` are bound to that meeting/chat.
2. If Alfred must join arbitrary meeting URLs, a Sandbox admin must
   grant tenant-wide Graph calling/media permissions such as
   `Calls.JoinGroupCall.All` and `Calls.AccessMedia.All`, re-consent
   the app, and likely open a Microsoft Teams / Graph Cloud
   Communications support case for app-hosted media tenant enablement.

## Symptom

Every `POST $BOT/api/calling/join` returns:

```
HTTP 502
{ "error": "Call join failed with tenant-level authorization constraints (7504/7505)",
  "errorCode": "CALL_JOIN_FAILED_7504_OR_7505" }
```

Microsoft Graph rejects `Calls.AddAsync` before reaching the bot's
audio socket. Persists across:
- Multiple meetings (different `JoinUrl`s, same error)
- All meeting types (private/scheduled/Meet-now)
- ~50h since policy bind (Microsoft's documented propagation window is ≤24h)

## What we've verified on OUR side (WDI subscription `e02c0038-…`)

| Gate | State |
|---|---|
| `Microsoft.BotService/.../channels/MsTeamsChannel.properties.properties.incomingCallRoute` | ✅ `graphPma` |
| `MsTeamsChannel.enableCalling` | ✅ `true` |
| `MsTeamsChannel.callingWebhook` | ✅ `https://alfred-disney-bot.eastus.cloudapp.azure.com/api/calling` |
| Bot `AppId` in manifest + appsettings | ✅ `207a38a4-67c5-4ef9-ada8-ea7998734d59` |
| Bot service / TLS / cert / firewall | ✅ healthy, `/api/calling/health` returns `200 Healthy` |
| Bot reaches `graph.microsoft.com` | ✅ all other Graph calls succeed |

Gate 1 (Bot Service channel config) is correct. The 7504/7505 is
Gate 2 — Sandbox tenant `CsApplicationAccessPolicy` enforcement.

## Most likely cause

The `CsApplicationAccessPolicy` policy is either:

1. **Not actually assigned** — `Grant-CsApplicationAccessPolicy` reported
   success but the assignment didn't write through.
2. **Scoped to specific users**, none of whom organized the meetings we
   probed against (the policy is enforced against the **meeting
   organizer's** policy, not the bot's caller).
3. **AppId mismatch** — the policy's `AppIds` list does not contain
   exactly `207a38a4-67c5-4ef9-ada8-ea7998734d59`.
4. **Tenant-side propagation hung** — re-grant kicks it loose.

We cannot diagnose any of these from outside Sandbox. We are NOT
admins in the Sandbox Entra/Teams tenant (`plutosdoghouse.com`,
tenant id `38387f0b-9a6f-46e2-8373-67422f8c2cb0`). We have RSC
permissions at install time, no admin role.

## Diagnostic commands (for a Sandbox Teams admin)

All of these need to run as a Sandbox Teams admin in PowerShell, NOT
from our side:

```powershell
# Connect to the Sandbox tenant (not WDI)
Connect-MicrosoftTeams -TenantId 38387f0b-9a6f-46e2-8373-67422f8c2cb0

# 1. Does the policy exist with OUR AppId?
Get-CsApplicationAccessPolicy | Where-Object {
    $_.AppIds -contains "207a38a4-67c5-4ef9-ada8-ea7998734d59"
} | Format-List Identity, Description, AppIds

# 2. Is it actually assigned to the test users + meeting organizers?
Get-CsUserPolicyAssignment `
    -Identity Logan.Robbins@plutosdoghouse.com `
    -PolicyType ApplicationAccessPolicy
Get-CsUserPolicyAssignment `
    -Identity Eric.Ortiz@plutosdoghouse.com `
    -PolicyType ApplicationAccessPolicy

# 3. If either is empty / wrong: re-apply with broadest scope
Grant-CsApplicationAccessPolicy `
    -PolicyName "AlfredOnlineMeetingsPolicy" -Global

# 4. Wait ≤24h. Verify on our side via:
#    curl -X POST $BOT/api/calling/join ...
#    Success = HTTP 200 with `call_id` in body, NOT 7504/7505.
```

## What we'll do once unblocked

The moment `/api/calling/join` returns `200 + call_id` on any sample
URL, the rest of the bot's calling path is ready — Gate 1 is correct,
the media socket initializes, and live audio + diarized STT flow
straight through into the existing dossier loop. No code changes
required on our side; this is purely a Sandbox-tenant
authorization-policy gate.

## Useful references

- README §7.1 "Call-join failures" — the symptom table we built from
  this exact issue.
- Microsoft docs:
  [Allow applications to access online meetings on behalf of a user](https://learn.microsoft.com/graph/cloud-communication-online-meeting-application-access-policy)
- The bot's calling webhook surface:
  [`/api/calling`](https://alfred-disney-bot.eastus.cloudapp.azure.com/api/calling) on `vm-alfred-disney`.
