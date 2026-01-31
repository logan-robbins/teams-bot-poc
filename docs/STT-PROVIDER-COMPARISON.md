# STT Provider Comparison for Meeting Transcription (2025/2026)

## Executive Summary

This document compares leading Speech-to-Text (STT) providers for real-time meeting transcription, focusing on output modes, streaming capabilities, diarization, and timestamps. Recommendations are provided for standardizing the FastAPI protocol to remain provider-agnostic.

---

## Comparison Table

| Provider | Streaming | Partial Results | Final Results | Diarization | Timestamps | Latency | Notes |
|----------|-----------|----------------|---------------|-------------|------------|---------|-------|
| **OpenAI Whisper/Realtime** | ✅ Yes | ✅ Yes (`transcript.text.delta`) | ✅ Yes (`transcript.text.done`) | ✅ Yes (GPT-4o Transcribe Diarize) | ✅ Word-level | ~200-500ms | Separate diarization model; supports verbose JSON with timestamps |
| **ElevenLabs Scribe v2** | ✅ Yes | ✅ Yes (partial transcripts) | ✅ Yes (committed transcripts) | ❌ No | ⚠️ Limited | **<150ms** | Ultra-low latency; manual commit control; 90+ languages; no native diarization |
| **Deepgram** | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes (`diarize=true`) | ✅ Word-level | ~100-300ms | Excellent diarization; `utterances=true` for logical segments; smart formatting |
| **AssemblyAI** | ✅ Yes | ✅ Yes | ✅ Yes | ❌ **No** (batch only) | ✅ Word-level | ~200-400ms | Streaming diarization not available; multichannel workaround possible |
| **Azure Speech** | ✅ Yes | ✅ Yes (`Recognizing`) | ✅ Yes (`Recognized`) | ✅ Yes (GA 2024) | ✅ Word-level | ~200-500ms | Currently implemented; real-time diarization with speaker IDs (GUEST1, GUEST2, etc.) |
| **Google Cloud Speech-to-Text** | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes (`diarization_config`) | ✅ Word-level | ~200-500ms | Chirp 3 model (2025); supports streaming diarization; 85+ languages |
| **AWS Transcribe** | ✅ Yes | ✅ Yes | ✅ Yes | ✅ Yes (`ShowSpeakerLabel`) | ✅ Word-level | ~200-500ms | Best with 2-5 speakers; max 30 speakers; speaker labels (spk_0, spk_1, etc.) |

---

## Detailed Feature Analysis

### 1. **OpenAI Whisper/Realtime API**
- **Streaming**: WebSocket-based real-time API
- **Output Modes**: 
  - Partial: `transcript.text.delta` events
  - Final: `transcript.text.segment` and `transcript.text.done` events
- **Diarization**: Via separate GPT-4o Transcribe Diarize model (not native to streaming)
- **Timestamps**: Included in verbose JSON output
- **Best For**: High accuracy needs, OpenAI ecosystem integration

### 2. **ElevenLabs Scribe v2 Realtime**
- **Streaming**: WebSocket-based, ultra-low latency
- **Output Modes**:
  - Partial: Real-time partial transcripts
  - Final: Committed transcripts (manual commit control available)
- **Diarization**: Not available
- **Timestamps**: Limited timestamp support
- **Best For**: Ultra-low latency requirements, multilingual (90+ languages), negative latency prediction

### 3. **Deepgram**
- **Streaming**: WebSocket-based streaming API
- **Output Modes**:
  - Partial: Interim results
  - Final: Finalized utterances
- **Diarization**: Native support with `diarize=true` parameter
- **Timestamps**: Word-level start/end times with speaker IDs
- **Best For**: Best-in-class diarization, developer-friendly API, smart formatting

### 4. **AssemblyAI**
- **Streaming**: Universal Streaming API
- **Output Modes**:
  - Partial: Partial transcripts
  - Final: Final transcripts
- **Diarization**: **Not available for streaming** (batch only)
- **Timestamps**: Word-level timestamps
- **Best For**: Simple streaming needs without diarization, or multichannel workarounds

### 5. **Azure Speech Services** ⭐ *Currently Implemented*
- **Streaming**: Continuous recognition API
- **Output Modes**:
  - Partial: `Recognizing` events
  - Final: `Recognized` events
- **Diarization**: Real-time diarization (GA May 2024) with speaker IDs
- **Timestamps**: Word-level timestamps
- **Best For**: Microsoft ecosystem, enterprise compliance, Teams integration

### 6. **Google Cloud Speech-to-Text**
- **Streaming**: gRPC streaming (`StreamingRecognize`)
- **Output Modes**:
  - Partial: Interim results
  - Final: Final results
- **Diarization**: Native support via `diarization_config` (Chirp 3 model, 2025)
- **Timestamps**: Word-level timestamps
- **Best For**: Google Cloud ecosystem, multilingual (85+ languages), latest model (Chirp 3)

### 7. **AWS Transcribe**
- **Streaming**: HTTP/2 or WebSocket (`StartStreamTranscription`)
- **Output Modes**:
  - Partial: Partial results
  - Final: Final results
- **Diarization**: Native support (`ShowSpeakerLabel=true`), best with 2-5 speakers
- **Timestamps**: Word-level timestamps with speaker labels
- **Best For**: AWS ecosystem, medical transcription (Transcribe Medical), enterprise scale

---

## Recommended Standardized Output Protocol

Based on the comparison, here are the recommended fields to standardize in your FastAPI protocol to remain provider-agnostic:

### Core Event Structure

```json
{
  "event_type": "partial" | "final" | "session_started" | "session_stopped" | "error",
  "text": "transcribed text or null",
  "timestamp_utc": "ISO 8601 UTC timestamp",
  "audio_start_ms": 0.0,
  "audio_end_ms": 0.0,
  "confidence": 0.0-1.0,
  "speaker_id": "speaker_0" | "GUEST1" | "spk_0" | null,
  "language": "en-US" | null,
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
    "provider": "azure_speech" | "deepgram" | "openai" | "elevenlabs" | "assemblyai" | "google" | "aws",
    "model": "whisper-large-v3" | "scribe-v2" | "nova-2" | "chirp-3" | null,
    "session_id": "uuid",
    "raw_response": {}
  },
  "error": {
    "code": "error_code",
    "message": "error message",
    "details": {}
  } | null
}
```

### Field Definitions

| Field | Type | Required | Description | Provider Mapping |
|-------|------|----------|-------------|------------------|
| `event_type` | string | ✅ Yes | Event type: `partial`, `final`, `session_started`, `session_stopped`, `error` | Maps to `recognizing`/`recognized` (Azure), `delta`/`done` (OpenAI), etc. |
| `text` | string \| null | ✅ Yes | Transcribed text (null for non-text events) | Direct mapping |
| `timestamp_utc` | string | ✅ Yes | ISO 8601 UTC timestamp of event | Provider timestamp normalized to UTC |
| `audio_start_ms` | float | ⚠️ Optional | Start time of audio segment in milliseconds | From provider timestamps |
| `audio_end_ms` | float | ⚠️ Optional | End time of audio segment in milliseconds | From provider timestamps |
| `confidence` | float | ⚠️ Optional | Confidence score (0.0-1.0) | Normalized from provider confidence |
| `speaker_id` | string \| null | ⚠️ Optional | Speaker identifier (null if diarization disabled) | Normalize: `GUEST1`→`speaker_0`, `spk_0`→`speaker_0` |
| `language` | string \| null | ⚠️ Optional | Detected language code (e.g., `en-US`) | From provider language detection |
| `words` | array | ⚠️ Optional | Word-level details (if available) | Extract from provider word-level data |
| `metadata.provider` | string | ✅ Yes | Provider identifier | Set by adapter |
| `metadata.model` | string \| null | ⚠️ Optional | Model name/version used | From provider response |
| `metadata.session_id` | string | ⚠️ Optional | Session identifier | Provider session ID |
| `metadata.raw_response` | object | ⚠️ Optional | Original provider response (for debugging) | Full provider response |
| `error` | object \| null | ⚠️ Optional | Error details (only for `error` events) | Normalized error structure |

### Provider-Specific Normalization

#### Azure Speech → Standard Format
```csharp
// Recognizing (partial)
event_type = "partial"
text = e.Result.Text
timestamp_utc = DateTime.UtcNow.ToString("O")
audio_start_ms = e.Result.OffsetInTicks / 10000.0  // Convert ticks to ms
audio_end_ms = (e.Result.OffsetInTicks + e.Result.Duration.Ticks) / 10000.0
speaker_id = e.Result.SpeakerId ?? null  // If diarization enabled
words = ExtractWords(e.Result)  // If word-level timestamps enabled
```

#### Deepgram → Standard Format
```python
# Partial result
event_type = "partial"
text = result.channel.alternatives[0].transcript
timestamp_utc = datetime.utcnow().isoformat() + "Z"
audio_start_ms = result.start * 1000
audio_end_ms = result.end * 1000
speaker_id = f"speaker_{result.channel.alternatives[0].words[0].speaker}" if diarize else None
words = [{"word": w.word, "start_ms": w.start*1000, "end_ms": w.end*1000, 
          "confidence": w.confidence, "speaker_id": f"speaker_{w.speaker}"} 
         for w in result.channel.alternatives[0].words]
```

#### OpenAI Realtime → Standard Format
```python
# transcript.text.delta (partial)
event_type = "partial"
text = event.delta
timestamp_utc = datetime.utcnow().isoformat() + "Z"
# Note: OpenAI Realtime may not provide word-level timestamps in delta events
```

---

## Recommendations

### 1. **Core Fields to Standardize** (Required)
- ✅ `event_type`: `partial` | `final` | `session_started` | `session_stopped` | `error`
- ✅ `text`: Transcribed text (null for non-text events)
- ✅ `timestamp_utc`: ISO 8601 UTC timestamp
- ✅ `metadata.provider`: Provider identifier

### 2. **Enhanced Fields** (Recommended for Diarization)
- ⚠️ `speaker_id`: Normalized speaker identifier (e.g., `speaker_0`, `speaker_1`)
- ⚠️ `audio_start_ms` / `audio_end_ms`: Segment timing
- ⚠️ `words`: Array of word-level details with timestamps and speaker IDs

### 3. **Optional Fields** (Nice to Have)
- ⚠️ `confidence`: Normalized confidence score
- ⚠️ `language`: Detected language code
- ⚠️ `metadata.model`: Model name/version
- ⚠️ `metadata.raw_response`: Original provider response (for debugging)

### 4. **Provider Selection Recommendations**

**For Ultra-Low Latency**: ElevenLabs Scribe v2 (<150ms)
- ⚠️ Trade-off: No diarization, limited timestamps

**For Best Diarization**: Deepgram or Azure Speech
- ✅ Deepgram: Best developer experience, excellent diarization
- ✅ Azure Speech: Already implemented, enterprise-ready

**For Multilingual**: Google Cloud (Chirp 3) or ElevenLabs
- ✅ Google: 85+ languages, latest model
- ✅ ElevenLabs: 90+ languages, ultra-low latency

**For Cost-Effective**: Deepgram or AssemblyAI
- ✅ Competitive pricing, good accuracy

**For Enterprise Compliance**: Azure Speech or AWS Transcribe
- ✅ HIPAA, SOC 2, GDPR compliance options

### 5. **Implementation Strategy**

1. **Update `TranscriptEvent` Model** (C#):
   ```csharp
   public record TranscriptEvent(
       string EventType,      // "partial" | "final" | "session_started" | "session_stopped" | "error"
       string? Text,
       string TimestampUtc,
       double? AudioStartMs = null,
       double? AudioEndMs = null,
       float? Confidence = null,
       string? SpeakerId = null,
       string? Language = null,
       List<WordDetail>? Words = null,
       EventMetadata? Metadata = null,
       EventError? Error = null
   );
   ```

2. **Create Provider Adapters**: Each provider implements `IRealtimeTranscriber` and normalizes output to `TranscriptEvent`

3. **Update Python FastAPI Endpoint**: Accept standardized format, handle optional fields gracefully

4. **Add Diarization Support**: Update factory to enable diarization when supported by provider

---

## Next Steps

1. ✅ Review and approve standardized protocol
2. ⏳ Update `TranscriptEvent` model to include new fields
3. ⏳ Create adapter pattern for provider normalization
4. ⏳ Update Python FastAPI endpoint to handle enhanced fields
5. ⏳ Implement Deepgram adapter as proof-of-concept
6. ⏳ Add diarization configuration to `BotConfiguration`

---

## References

- [OpenAI Realtime API](https://platform.openai.com/docs/guides/realtime-transcription)
- [ElevenLabs Scribe v2](https://elevenlabs.io/blog/introducing-scribe-v2-realtime)
- [Deepgram Streaming API](https://developers.deepgram.com/reference/speech-to-text-api/listen-streaming)
- [Azure Speech Diarization](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/get-started-stt-diarization)
- [Google Cloud Speech-to-Text Chirp 3](https://docs.cloud.google.com/speech-to-text/docs/release-notes)
- [AWS Transcribe Streaming](https://docs.aws.amazon.com/transcribe/latest/dg/conversation-diarization-streaming-med.html)
