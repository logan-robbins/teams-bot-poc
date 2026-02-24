# LegionMeet

LegionMeet is a Teams meeting agent platform with three runtime pieces:
- C# Teams media bot (`src/`)
- Python transcript sink + analysis runtime (`python/`)
- Streamlit operator UI (`python/`)

This README is intentionally constrained:
- One architecture section for humans
- Immutable entry points and commands for LLM/operator execution
- No environment-specific domains, IPs, credentials, or volatile config examples

## System Architecture

```text
Teams Meeting
    ->
Bot HTTP Webhook/API
    ->
C# Media Bot Runtime (join call, receive audio, publish transcript events)
    ->
STT Provider (selected by active runtime config)
    ->
Python Transcript Sink (session state + analysis orchestration)
    ->
Analysis Engine + Output Router
    ->
Streamlit UI and/or external output routes
```

## Immutable Project Roots

| Path | Purpose |
|---|---|
| `src/` | C# Teams bot runtime and HTTP API |
| `python/` | Sink, UI, analysis packages, tests, and lockfile |
| `scripts/` | Deployment and operations entry scripts |
| `manifest/` | Teams app manifest and package assets |

## Immutable Entry Paths

| Path | Purpose |
|---|---|
| `src/Program.cs` | C# process entrypoint |
| `src/Controllers/CallingController.cs` | Bot HTTP endpoints |
| `src/Services/TeamsCallingBotService.cs` | Call lifecycle orchestration |
| `src/Services/PythonTranscriptPublisher.cs` | Bot -> sink transcript forwarding |
| `python/transcript_sink.py` | FastAPI sink application |
| `python/run_variant_sink.py` | Canonical sink launcher |
| `python/streamlit_ui.py` | Streamlit UI app |
| `python/run_variant_ui.py` | Canonical UI launcher |
| `python/interview_agent/` | Analysis/session package |
| `python/legionmeet_platform/` | Product spec + routing package |
| `python/tests/` | Python test suite |
| `manifest/manifest.json` | Teams app manifest |

## Canonical Commands

### Python dependency sync

```bash
cd python
uv sync
```

### Local runtime (canonical launchers)

Terminal 1:

```bash
cd python
uv run python run_variant_sink.py --instance <instance-id> --port <sink-port> --product-spec <spec-path>
```

Terminal 2:

```bash
cd python
uv run python run_variant_ui.py --instance <instance-id> --port <ui-port> --sink-url http://127.0.0.1:<sink-port> --product-spec <spec-path>
```

### Python tests

```bash
cd python
uv run pytest tests -v
```

### C# build

```bash
cd src
dotnet restore
dotnet build --configuration Release
```

### C# run (explicit config path)

macOS/Linux shell:

```bash
cd src/bin/Release/net8.0
./TeamsMediaBot --config <absolute-config-path>
```

Windows PowerShell:

```powershell
cd src\bin\Release\net8.0
.\TeamsMediaBot.exe --config <absolute-config-path>
```

### Bot HTTP endpoints

Health:

```bash
curl <bot-base-url>/api/calling/health
```

Join meeting:

```bash
curl -X POST <bot-base-url>/api/calling/join \
  -H "Content-Type: application/json" \
  -d '{
    "joinUrl":"<teams-meeting-join-url>",
    "displayName":"<bot-display-name>",
    "meetingId":"<external-meeting-id>",
    "organizerTenantId":"<tenant-id>",
    "botAttendeePresent":true,
    "joinMode":"invite_and_graph_join"
  }'
```

Policy-mode request (deferred until Teams auto-invites bot):

```bash
curl -X POST <bot-base-url>/api/calling/join \
  -H "Content-Type: application/json" \
  -d '{
    "joinUrl":"<teams-meeting-join-url>",
    "meetingId":"<external-meeting-id>",
    "botAttendeePresent":true,
    "joinMode":"policy_auto_invite"
  }'
```

Join response behavior:
- `200 OK`: explicit Graph join started, response includes `callId`
- `202 Accepted`: policy mode selected; join is deferred awaiting incoming call webhook
- `400 Bad Request`: failed fast (for example `BOT_NOT_INVITED`)
- `403 Forbidden`: tenant/mode/permission issue (`TENANT_NOT_ENABLED_FOR_MODE`, `GRAPH_PERMISSION_MISSING`)
- `502 Bad Gateway`: Graph join failed with known tenant authorization class (`CALL_JOIN_FAILED_7504_OR_7505`)

## Join Mode Configuration

The C# runtime reads optional `JoinMode` settings from bot config JSON.

Supported modes:
- `policy_auto_invite`
- `invite_and_graph_join`

Config keys:
- `JoinMode.PreferredMode`
- `JoinMode.PolicyAutoInviteEnabled`
- `JoinMode.AutoFallbackToInviteAndGraphJoin`
- `JoinMode.RequireBotAttendeeForInviteJoin`
- `JoinMode.TenantOverrides.<tenant-id>.*`

### Teams app package

```bash
cd manifest
zip -r teams-bot-poc.zip manifest.json color.png outline.png
```

## Deployment Script Entrypoints

Run from `scripts/`:

```bash
./deploy-azure-vm.sh
./deploy-azure-agent.sh
```

PowerShell entrypoints:

```bash
pwsh ./deploy-production.ps1
pwsh ./update-bot.ps1
pwsh ./diagnose-bot.ps1
```

## LLM Code-Finder Index

Use this table to jump directly to the change surface:

| If you need to change... | Start here |
|---|---|
| Bot startup/runtime wiring | `src/Program.cs` |
| Webhook/join/health API behavior | `src/Controllers/CallingController.cs` |
| Call join/leave/media handling | `src/Services/TeamsCallingBotService.cs` |
| Per-call audio buffering and forwarding | `src/Services/CallHandler.cs` |
| Transcript event contract (C#) | `src/Models/TranscriptEvent.cs` |
| Sink ingest endpoints/state flow | `python/transcript_sink.py` |
| Analysis behavior/session logic | `python/interview_agent/` |
| Product spec validation/loading | `python/legionmeet_platform/spec_loader.py` |
| Output route dispatch | `python/legionmeet_platform/routes/router.py` |
| UI rendering + simulation controls | `python/streamlit_ui.py` |
| Multi-instance launcher behavior | `python/run_variant_sink.py`, `python/run_variant_ui.py` |
| Python tests and expected behavior | `python/tests/` |
| Teams app manifest fields | `manifest/manifest.json` |

## Maintenance Rule

When an immutable entry path or canonical command changes, update this README in the same change set.
