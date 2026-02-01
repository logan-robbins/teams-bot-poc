# PLAN: Teams Meeting Transcription → Agentic Python App

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       Microsoft Teams Meeting                            │
│                     (Audio from participants)                            │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │ Raw audio frames (PCM 16kHz mono)
                                 ↓
┌─────────────────────────────────────────────────────────────────────────┐
│                  C# Bot (TeamsMediaBot)                                  │
│                                                                          │
│  CallHandler.cs → IRealtimeTranscriber → PythonTranscriptPublisher.cs   │
│                           │                                              │
│                    ┌──────┴──────┐                                       │
│                    │ STT Provider │                                      │
│                    │  (pluggable) │                                      │
│                    └──────┬──────┘                                       │
│                           │                                              │
│  Supported providers:     │                                              │
│  • Azure Speech ⭐        │ ← Currently implemented                      │
│  • Deepgram              │ ← Best diarization                           │
│  • Google Cloud (Chirp)  │ ← Diarization support                        │
│  • AWS Transcribe        │ ← Diarization support                        │
│  • OpenAI Realtime       │ ← Separate diarization model                 │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │ HTTP POST (JSON)
                                 │ Provider-agnostic TranscriptEvent
                                 ↓
┌─────────────────────────────────────────────────────────────────────────┐
│              Python FastAPI Sink (transcript_sink.py)                    │
│                                                                          │
│  POST /transcript → transcript_queue → agent_processing_loop()          │
│                                                                          │
│  Integration point for:                                                  │
│  • LangChain/LangGraph agents                                           │
│  • Custom agentic frameworks                                            │
│  • Vector DB storage                                                    │
│  • Real-time meeting assistants                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

## Scope (POC)

- English-only recognition (`en-US`)
- Meeting-scoped processing (no cross-meeting memory)
- One STT provider per call (config-driven selection)
- Transcript sink: internal, no auth, best-effort delivery
- Agent ingestion: **final** transcript events only (ignore partials)

## Source of Truth (repo files)

| Component | File |
|-----------|------|
| STT provider interface | `src/Services/IRealtimeTranscriber.cs` |
| Azure Speech implementation | `src/Services/AzureSpeechRealtimeTranscriber.cs` |
| Transcript event model (C#) | `src/Models/TranscriptEvent.cs` |
| HTTP publisher (C#) | `src/Services/PythonTranscriptPublisher.cs` |
| STT/provider config | `src/Models/BotConfiguration.cs` |
| Python FastAPI sink | `python/transcript_sink.py` |
| Endpoint config | `src/Config/appsettings.json` |
| Provider comparison | `docs/STT-PROVIDER-COMPARISON.md` |
| Provider shortlist | `docs/STT-SHORTLIST.md` |

## Transcript Event Contract

### Current (v0) — Minimal

```json
{
  "Kind": "recognizing" | "recognized" | "session_started" | "session_stopped" | "canceled",
  "Text": "transcript text" | null,
  "TsUtc": "2026-01-30T12:34:56.789Z",
  "Details": "error details" | null
}
```

C# source: `src/Models/TranscriptEvent.cs`

### Target (v1) — Provider-Agnostic with Diarization

```json
{
  "event_type": "partial" | "final" | "session_started" | "session_stopped" | "error",
  "text": "transcript text" | null,
  "timestamp_utc": "2026-01-30T12:34:56.789Z",
  "speaker_id": "speaker_0" | null,
  "audio_start_ms": 0.0,
  "audio_end_ms": 500.0,
  "confidence": 0.95,
  "words": [
    {
      "word": "Hello",
      "start_ms": 0.0,
      "end_ms": 500.0,
      "confidence": 0.95,
      "speaker_id": "speaker_0"
    }
  ],
  "metadata": {
    "provider": "azure_speech" | "deepgram" | "openai" | "google" | "aws",
    "model": "model-name" | null,
    "session_id": "uuid"
  },
  "error": {
    "code": "error_code",
    "message": "error message"
  } | null
}
```

### Field Requirements

| Field | Required | Description |
|-------|----------|-------------|
| `event_type` | ✅ | Event type (maps from provider-specific: `recognizing`→`partial`, `recognized`→`final`) |
| `text` | ✅ | Transcribed text (null for non-text events) |
| `timestamp_utc` | ✅ | ISO 8601 UTC timestamp |
| `metadata.provider` | ✅ | Provider identifier |
| `speaker_id` | ⚠️ | Normalized speaker ID (`speaker_0`, `speaker_1`). Null if diarization disabled. |
| `audio_start_ms` | ⚠️ | Segment start time in milliseconds |
| `audio_end_ms` | ⚠️ | Segment end time in milliseconds |
| `confidence` | ⚠️ | Normalized 0.0-1.0 score |
| `words` | ⚠️ | Word-level details with timestamps and speaker IDs |

### Speaker ID Normalization

| Provider | Raw Format | Normalized |
|----------|------------|------------|
| Azure Speech | `GUEST1`, `GUEST2` | `speaker_0`, `speaker_1` |
| Deepgram | `0`, `1` | `speaker_0`, `speaker_1` |
| AWS Transcribe | `spk_0`, `spk_1` | `speaker_0`, `speaker_1` |
| Google Cloud | Speaker numbers | `speaker_0`, `speaker_1` |
| OpenAI | Via separate model | `speaker_0`, `speaker_1` |

## STT Provider Abstraction

### Interface (C#)

```csharp
// src/Services/IRealtimeTranscriber.cs
public interface IRealtimeTranscriber : IAsyncDisposable
{
    Task StartAsync(CancellationToken ct = default);
    void PushPcm16k16bitMono(ReadOnlySpan<byte> pcmFrame);
    Task StopAsync();
}
```

### Adding a New Provider

1. Create `src/Services/{Provider}RealtimeTranscriber.cs` implementing `IRealtimeTranscriber`
2. Add provider config class to `src/Models/BotConfiguration.cs`
3. Update factory/DI registration in `src/Program.cs`
4. Normalize output to standard `TranscriptEvent` format
5. Add provider key to `appsettings.json` under `Stt.Provider`

### Provider Selection (appsettings.json)

```json
{
  "Stt": {
    "Provider": "AzureSpeech",
    "AzureSpeech": {
      "Key": "...",
      "Region": "eastus",
      "RecognitionLanguage": "en-US",
      "EndpointId": null
    },
    "Deepgram": {
      "ApiKey": "...",
      "Model": "nova-2",
      "Diarize": true
    }
  }
}
```

## Diarization (Speaker Detection)

### Providers with Streaming Diarization

| Provider | Streaming Diarization | Notes |
|----------|----------------------|-------|
| **Deepgram** | ✅ Native | `diarize=true`, best-in-class |
| **Azure Speech** | ✅ Native | GA May 2024, `GUEST1`/`GUEST2` format |
| **Google Cloud** | ✅ Native | Chirp 3 model, `diarization_config` |
| **AWS Transcribe** | ✅ Native | Best with 2-5 speakers, max 30 |
| **OpenAI** | ⚠️ Separate model | Requires GPT-4o Transcribe Diarize model |

### Enabling Diarization

Provider-specific. See `docs/STT-PROVIDER-COMPARISON.md` for implementation details.

## Agent Integration (Python)

### Integration Point

`python/transcript_sink.py` → `agent_processing_loop()`:

```python
async def agent_processing_loop():
    while True:
        evt = await transcript_queue.get()
        kind = evt["kind"]
        text = evt["text"]
        
        # Only process final transcripts
        if kind == "recognized" and text:
            # YOUR AGENT INTEGRATION HERE
            # await agent.process(text)
            # await langchain_agent.arun(text)
            pass
```

### Chunking Rule

- Process `event_type == "final"` (or `kind == "recognized"`) with non-empty text
- Ignore `event_type == "partial"` (or `kind == "recognizing"`) — unstable/noisy

## Contract Changes (Strict)

If you add/rename/remove fields in `TranscriptEvent`, update ALL of:

1. `src/Models/TranscriptEvent.cs`
2. `src/Services/PythonTranscriptPublisher.cs`
3. `python/transcript_sink.py`

The Python sink accepts both PascalCase (`Kind`) and lowercase (`kind`) for backwards compatibility.

## Smoke Test

### Run Python Sink

```bash
cd teams-bot-poc/python
uv venv
uv pip install -r requirements.txt
uv run transcript_sink.py
```

### Send Test Event (v0 format)

```bash
curl -X POST http://127.0.0.1:8765/transcript \
  -H "Content-Type: application/json" \
  -d '{"Kind":"recognized","Text":"hello world","TsUtc":"2026-01-01T00:00:00Z","Details":null}'
```

### Send Test Event (v1 format with diarization)

```bash
curl -X POST http://127.0.0.1:8765/transcript \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "final",
    "text": "Hello, this is speaker zero.",
    "timestamp_utc": "2026-01-30T12:34:56.789Z",
    "speaker_id": "speaker_0",
    "audio_start_ms": 0.0,
    "audio_end_ms": 2500.0,
    "confidence": 0.95,
    "metadata": {"provider": "deepgram", "model": "nova-2"}
  }'
```

### Verify

```bash
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/stats
```

## Implementation Checklist

### v0 → v1 Contract Migration

- [ ] Update `TranscriptEvent.cs` with new fields (speaker_id, audio_start_ms, etc.)
- [ ] Update `PythonTranscriptPublisher.cs` to serialize new fields
- [ ] Update `transcript_sink.py` to parse new fields
- [ ] Add speaker ID normalization logic per provider

### New Provider Implementation

- [ ] Implement `IRealtimeTranscriber` for target provider
- [ ] Add provider config to `BotConfiguration.cs`
- [ ] Wire up DI/factory in `Program.cs`
- [ ] Normalize events to standard `TranscriptEvent` format
- [ ] Test with smoke test commands
- [ ] Document provider-specific config in README

### Diarization Support

- [ ] Add `Diarize: bool` flag to relevant provider configs
- [ ] Implement speaker ID extraction per provider
- [ ] Normalize all speaker IDs to `speaker_N` format
- [ ] Update Python sink to handle/log speaker changes
