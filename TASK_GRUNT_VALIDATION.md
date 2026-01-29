# GRUNT VALIDATION: Teams Media Bot vs Microsoft Samples

**Created:** 2026-01-29  
**Status:** ✅ COMPLETE

## Issues Found and Status

### 1. ✅ FIXED - Webhook Notification Handling
- **Problem:** Controller just returned Ok() without processing
- **Impact:** SDK events never fired, transcriber never started
- **Fix:** Now calls `ProcessNotificationAsync` per Microsoft sample pattern
- **File:** `Controllers/CallingController.cs`

### 2. ✅ FIXED - Authentication Provider
- **Problem:** No inbound JWT validation, no token caching
- **Impact:** Security vulnerability, performance issues
- **Fix:** Production-grade JWT validation with OpenID Connect
- **File:** `Services/TeamsCallingBotService.cs` (AuthenticationProvider class)

### 3. ✅ FIXED - Missing Microsoft.Skype.Bots.Media Package
- **Problem:** Package not explicitly referenced
- **Impact:** May compile but will fail at runtime
- **Fix:** Added `<PackageReference Include="Microsoft.Skype.Bots.Media" Version="1.27.0.2-alpha" />`
- **File:** `TeamsMediaBot.csproj`

### 4. ✅ FIXED - Non-Thread-Safe Dictionary
- **Problem:** `Dictionary<string, CallContext>` used for concurrent access
- **Impact:** Race conditions, potential crashes
- **Fix:** Replaced with `ConcurrentDictionary<string, CallHandler>`
- **File:** `Services/TeamsCallingBotService.cs`

### 5. ✅ FIXED - Missing Heartbeat/Keepalive
- **Problem:** No `Call.KeepAliveAsync()` implemented
- **Impact:** Calls would terminate after 45 minutes
- **Fix:** Implemented HeartbeatHandler with 10-minute keepalive interval
- **Files:** `Services/HeartbeatHandler.cs`, `Services/CallHandler.cs`

### 6. ✅ FIXED - Missing Global Call Event Subscriptions
- **Problem:** No `Client.Calls().OnIncoming` / `OnUpdated` handlers
- **Impact:** SDK state management incomplete
- **Fix:** Added global event subscriptions in InitializeAsync
- **File:** `Services/TeamsCallingBotService.cs`

### 7. ✅ FIXED - Missing VideoSocketSettings
- **Problem:** Only AudioSocketSettings passed to CreateMediaSession
- **Impact:** May cause SDK issues even if video not used
- **Fix:** Added VideoSocketSettings with StreamDirection.Inactive
- **File:** `Services/TeamsCallingBotService.cs` (CreateMediaSession method)

## Files Created/Modified

### New Files:
- `Services/HeartbeatHandler.cs` - Base class for heartbeat keepalive
- `Services/CallHandler.cs` - Per-call lifecycle management with heartbeat

### Modified Files:
- `TeamsMediaBot.csproj` - Added JWT packages and Skype.Bots.Media
- `Controllers/CallingController.cs` - Fixed notification processing
- `Services/TeamsCallingBotService.cs` - Major refactor with all fixes

## Architecture Changes

**Before:** Single-threaded, no heartbeat, inline event handlers
**After:** Thread-safe, 10-minute heartbeat, CallHandler pattern (matches Microsoft samples)

## Testing
After deployment, verify:
1. Bot joins meeting successfully
2. "Call established" appears in logs
3. Audio frames received (~50/sec)
4. Transcription events published to Python
5. Calls don't disconnect after 45 minutes (heartbeat working)

---

## Phase 2: Package Version Alignment (2026-01-29)

### 8. ✅ FIXED - NuGet Package Version Conflicts
- **Problem:** Package downgrade errors during restore (NU1605)
- **Root cause:** Our SDK versions (1.2.0.15690) require newer transitive deps
- **Fix:** Updated all packages to match transitive requirements:
  - `Microsoft.Skype.Bots.Media` → 1.32.0.70-preview (native .NET 8.0)
  - `Microsoft.Graph` → 5.92.0
  - `Microsoft.IdentityModel.*` → 8.6.1
- **File:** `TeamsMediaBot.csproj`

### 9. ✅ FIXED - Missing Microsoft.Graph.Contracts Using
- **Problem:** Extension methods like `GetTenantId()`, `SetTenantId()` need this namespace
- **Fix:** Added `using Microsoft.Graph.Contracts;` per EchoBot pattern
- **Files:** `TeamsCallingBotService.cs`, `CallHandler.cs`

### 10. ✅ FIXED - Missing Microsoft.Skype.Bots.Media Using
- **Problem:** `AudioMediaReceivedEventArgs` not found
- **Fix:** Added `using Microsoft.Skype.Bots.Media;`
- **File:** `CallHandler.cs`

### 11. ✅ FIXED - Media SDK Namespace + API Differences
- **Problem:** `MediaPlatformSettings`, `AudioSocketSettings`, `StreamDirection`, and `AudioFormat` not resolved; `ICommunicationsClient.StartAsync/DisposeAsync` missing
- **Fix:** Updated usings to pull media types from `Microsoft.Skype.Bots.Media` and removed StartAsync/DisposeAsync calls
- **Files:** `TeamsCallingBotService.cs`, `CallingController.cs`

### 12. ✅ FIXED - Build Warnings Cleanup
- **Problem:** CS8602 null dereference in inbound validation; NETSDK1206 from transitive SQLitePCLRaw alpine RIDs
- **Fix:** Added null guard in ValidateInboundRequestAsync and suppressed NETSDK1206 in csproj
- **Files:** `TeamsCallingBotService.cs`, `TeamsMediaBot.csproj`

## Final Package Configuration (reference)
- `Microsoft.Graph.Communications.Calls.Media` 1.2.0.15690
- `Microsoft.Skype.Bots.Media` 1.32.0.70-preview
- `Microsoft.Graph` 5.92.0
- `Microsoft.IdentityModel.Protocols.OpenIdConnect` 8.6.1
- `System.IdentityModel.Tokens.Jwt` 8.6.1

## Build Command
- `cd C:\teams-bot-poc\src`
- `dotnet restore`
- `dotnet build --configuration Release`
