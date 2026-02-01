# STT Provider Shortlist for Meeting Transcription (2025/2026)

All providers listed support streaming diarization (speaker detection).

## Top Contenders

| Provider | Latency | Diarization | Streaming | Best For |
|----------|---------|-------------|-----------|----------|
| **Deepgram** | ~100-300ms | ✅ **Excellent** | ✅ Yes | Best diarization, developer-friendly |
| **Azure Speech** ⭐ | ~200-500ms | ✅ Yes (GA) | ✅ Yes | Currently implemented, enterprise |
| **Google Cloud (Chirp 3)** | ~200-500ms | ✅ Yes | ✅ Yes | Latest model (2025), 85+ languages |
| **AWS Transcribe** | ~200-500ms | ✅ Yes (2-5 speakers) | ✅ Yes | AWS ecosystem, medical |
| **OpenAI Whisper/Realtime** | ~200-500ms | ⚠️ Separate model | ✅ Yes | High accuracy, OpenAI ecosystem |

⭐ = Currently implemented in codebase

---

## Output Modes Comparison

| Provider | Partial Results | Final Results | Word Timestamps | Speaker IDs |
|----------|----------------|---------------|----------------|-------------|
| Deepgram | ✅ Interim | ✅ Finalized | ✅ Word-level | ✅ `speaker_0`, `speaker_1` |
| Azure Speech | ✅ `Recognizing` | ✅ `Recognized` | ✅ Word-level | ✅ `GUEST1`, `GUEST2` |
| Google Cloud | ✅ Interim | ✅ Final | ✅ Word-level | ✅ Speaker numbers |
| AWS Transcribe | ✅ Partial | ✅ Final | ✅ Word-level | ✅ `spk_0`, `spk_1` |
| OpenAI | ✅ `delta` events | ✅ `done` events | ✅ Word-level | ⚠️ Via separate model |

---

## Recommended Standardized Protocol Fields

### Required Fields
```json
{
  "event_type": "partial" | "final" | "session_started" | "session_stopped" | "error",
  "text": "transcribed text or null",
  "timestamp_utc": "2026-01-30T12:34:56.789Z",
  "metadata": {
    "provider": "azure_speech" | "deepgram" | "openai" | "google" | "aws"
  }
}
```

### Recommended Enhanced Fields (for Diarization)
```json
{
  "speaker_id": "speaker_0" | "speaker_1" | null,
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
  ]
}
```

### Provider-Specific Normalization
- **Azure**: `GUEST1` → `speaker_0`, `GUEST2` → `speaker_1`
- **Deepgram**: `0` → `speaker_0`, `1` → `speaker_1`
- **AWS**: `spk_0` → `speaker_0`, `spk_1` → `speaker_1`
- **Google**: Speaker numbers → `speaker_0`, `speaker_1`

---

## Recommendation: Standardize These Fields

### Core (Required)
1. ✅ `event_type`: `partial` | `final` | `session_started` | `session_stopped` | `error`
2. ✅ `text`: Transcribed text (null for non-text events)
3. ✅ `timestamp_utc`: ISO 8601 UTC timestamp
4. ✅ `metadata.provider`: Provider identifier

### Enhanced (Recommended for Diarization Support)
5. ⚠️ `speaker_id`: Normalized to `speaker_0`, `speaker_1`, etc. (null if diarization disabled)
6. ⚠️ `audio_start_ms` / `audio_end_ms`: Segment timing in milliseconds
7. ⚠️ `words`: Array of word-level details (optional, but enables rich features)

### Optional (Nice to Have)
8. ⚠️ `confidence`: Normalized 0.0-1.0 score
9. ⚠️ `language`: Detected language code (e.g., `en-US`)
10. ⚠️ `metadata.model`: Model name/version
11. ⚠️ `metadata.raw_response`: Original provider response (for debugging)

---

## Quick Decision Matrix

**Choose Deepgram if**: You need best-in-class diarization and developer experience
**Choose Azure Speech if**: You're already using it (current implementation) and need enterprise compliance
**Choose Google Cloud if**: You need latest model (Chirp 3) and multilingual support
**Choose AWS if**: You're on AWS and need medical transcription features
**Choose OpenAI if**: You're in the OpenAI ecosystem and need high accuracy
