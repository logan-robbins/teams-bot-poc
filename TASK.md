# TASK: Interview Analysis System Implementation

**Created:** 2026-01-31  
**Status:** COMPLETE  
**Tests:** 67/67 passing

---

## Architecture

```
C# Bot (Azure Windows VM)     Python Agent (External Server)
teamsbot.qmachina.com         agent.qmachina.com
      │                              │
      │ POST /transcript             │
      └─────────────────────────────►│ FastAPI Sink v2
                                     │   ├─ Session Management
                                     │   ├─ Speaker Diarization
                                     │   └─ InterviewAnalyzer (OpenAI Agents SDK)
                                     │
                                     └──► output/{session_id}_analysis.json
```

---

## Completed Items

### 1. [COMPLETE] Interview Analysis Agent (`python/interview_agent/`)
- `agent.py` - OpenAI Agents SDK interview analyzer
- `__init__.py` - Package exports
- `models.py` - TranscriptEvent, AnalysisItem, SessionAnalysis
- `session.py` - InterviewSessionManager
- `output.py` - AnalysisOutputWriter

### 2. [COMPLETE] Enhanced Transcript Sink (`python/transcript_sink.py`)
- v2 diarization format (speaker_id, audio_start_ms, etc.)
- Session endpoints: `/session/start`, `/session/end`, `/session/status`, `/session/map-speaker`
- Agent integration with background processing loop
- v1 to v2 format normalization for C# bot compatibility

### 3. [COMPLETE] Auto-Join Scripts
- `scripts/auto_join.py` - Python auto-join script
- `scripts/auto_join.ps1` - PowerShell wrapper for Windows Task Scheduler

### 4. [COMPLETE] Mock Data & Tests
- `tests/mock_data.py` - Realistic TranscriptEvent generator
- `tests/test_interview.py` - Session/analysis tests (40 tests)
- `tests/test_sink.py` - FastAPI endpoint tests (27 tests)

---

## Configuration Required

### C# Bot (`src/Config/appsettings.json`)
```json
"TranscriptSink": {
  "PythonEndpoint": "https://agent.qmachina.com/transcript"
}
```

### DNS
```
agent.qmachina.com  A  <PYTHON_SERVER_IP>
```

### Environment Variable
```bash
export OPENAI_API_KEY="sk-..."
```

---

## Running Locally

```bash
cd /Users/loganrobbins/research/teams/teams-bot-poc/python

# Install dependencies
uv sync

# Run transcript sink
OPENAI_API_KEY=sk-... uv run python transcript_sink.py

# Run tests
uv run pytest tests/ -v
```

---

## Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/transcript` | Receive transcript events (v1/v2) |
| POST | `/session/start` | Start interview session |
| POST | `/session/map-speaker` | Map speaker_id to role |
| GET | `/session/status` | Get session info |
| POST | `/session/end` | End session, finalize analysis |
| GET | `/health` | Health check |
| GET | `/stats` | Statistics |

---

## Test Data Format

```json
{
  "event_type": "final",
  "text": "I have 5 years of Python experience...",
  "timestamp_utc": "2026-01-31T12:34:56.789Z",
  "speaker_id": "speaker_0",
  "audio_start_ms": 1000.0,
  "audio_end_ms": 2500.0,
  "confidence": 0.95,
  "metadata": {"provider": "deepgram", "model": "nova-3"}
}
```

---

## Next Steps (Post-Implementation)

1. Deploy Python app to cloud server with TLS (Caddy/nginx)
2. Update C# appsettings.json with external FQDN
3. Create DNS record for agent.qmachina.com
4. Set OPENAI_API_KEY on deployment server
