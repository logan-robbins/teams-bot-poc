# IMPLEMENTATION GUIDE: Teams Meeting → Diarized Transcription → Python Agent

**Version**: 2.1 (2026-02-01)  
**Status**: Python COMPLETE ✅ | C# PENDING (requires Windows/.NET)  
**Documentation Sources**: All implementations MUST use 2025/2026 documentation only

---

## IMPLEMENTATION STATUS

| Step | Component | Status | Notes |
|------|-----------|--------|-------|
| 7 | Python Transcript Sink | ✅ COMPLETE | Exceeds spec with session mgmt, agent integration |
| 10 | Python requirements.txt | ✅ COMPLETE | Using pyproject.toml with proper deps |
| 1-6 | C# Bot Code | ⏳ PENDING | Requires Windows/.NET environment |
| 8-9 | C# Configuration | ⏳ PENDING | Requires Windows/.NET environment |

**Python Validation (2026-02-01):**
- 67/67 tests pass
- End-to-end simulation successful
- Agent gracefully degrades without OPENAI_API_KEY

---

## PURPOSE

Build a Teams bot that:
1. Auto-joins meetings via URL
2. Streams live audio (diarized) to identify **who is speaking**
3. POSTs transcript events to a Python endpoint
4. Python agent reacts in real-time (separate UI via Streamlit v1)

**The bot does NOT speak or type back into the meeting.**

---

## CRITICAL DECISION: STT Provider

### CHOSEN: Deepgram (Primary) + Azure Speech ConversationTranscriber (Secondary)

**Why Deepgram**:
- Best-in-class streaming diarization (~100-300ms latency)
- Simple: `diarize=true` parameter
- Speaker IDs on every word in real-time
- C# SDK: `dotnet add package Deepgram`

**Why Azure ConversationTranscriber as fallback**:
- Already have Azure Speech resource deployed
- GA May 2024 real-time diarization
- Uses `ConversationTranscriber` class (NOT `SpeechRecognizer`)

### CRITICAL BUG IN CURRENT IMPLEMENTATION

The existing `AzureSpeechRealtimeTranscriber.cs` uses `SpeechRecognizer`. **This class does NOT support real-time diarization**.

To get speaker IDs with Azure Speech, you MUST use:
```csharp
// WRONG - No diarization
var recognizer = new SpeechRecognizer(speechConfig, audioConfig);

// CORRECT - Has diarization (Speaker IDs: Guest-1, Guest-2, etc.)
var conversationTranscriber = new ConversationTranscriber(speechConfig, audioConfig);
```

**Source**: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/get-started-stt-diarization

---

## ARCHITECTURE

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Microsoft Teams Meeting                              │
│                        (Multiple participants)                               │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 │ Graph Communications SDK
                                 │ 50 fps × 20ms frames = 16kHz/16-bit/mono PCM
                                 ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│               C# TeamsMediaBot (Azure Windows VM)                            │
│                                                                              │
│  CallHandler.cs → IRealtimeTranscriber                                      │
│                          │                                                   │
│           ┌──────────────┴──────────────┐                                   │
│           │                              │                                   │
│   DeepgramRealtimeTranscriber    AzureConversationTranscriber               │
│   (PRIMARY - Best diarization)   (FALLBACK - Enterprise)                    │
│           │                              │                                   │
│           └──────────────┬──────────────┘                                   │
│                          │ Normalized TranscriptEvent                       │
│                          ↓                                                   │
│                PythonTranscriptPublisher.cs                                 │
│                   POST → http://127.0.0.1:8765/transcript                   │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 │ JSON (with speaker_id, timestamps)
                                 ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│              Python FastAPI Sink (transcript_sink.py)                        │
│                                                                              │
│  POST /transcript → validate → transcript_queue → agent_processing_loop()   │
│                                                                              │
│  Agentic Integration Point:                                                  │
│  • Filter: event_type == "final" AND speaker_id != null                     │
│  • Forward to: LangGraph / Custom Agent / MCP Server                        │
│                                                                              │
│  Separate Streamlit UI (v1) displays agent responses                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## IMPLEMENTATION STEPS (Ordered)

### STEP 1: Update TranscriptEvent Contract

**File**: `src/Models/TranscriptEvent.cs`

Replace the entire file with:

```csharp
namespace TeamsMediaBot.Models;

/// <summary>
/// Provider-agnostic transcript event with diarization support.
/// Normalized from Deepgram/Azure/etc to this common format.
/// 
/// Last Grunted: 01/31/2026 12:00:00 PM PST
/// </summary>
public record TranscriptEvent(
    /// <summary>"partial" | "final" | "session_started" | "session_stopped" | "error"</summary>
    string EventType,
    
    /// <summary>Transcribed text (null for non-text events)</summary>
    string? Text,
    
    /// <summary>ISO 8601 UTC timestamp</summary>
    string TimestampUtc,
    
    /// <summary>Normalized speaker ID: "speaker_0", "speaker_1", etc. Null if diarization disabled.</summary>
    string? SpeakerId = null,
    
    /// <summary>Segment start time in milliseconds from audio stream start</summary>
    double? AudioStartMs = null,
    
    /// <summary>Segment end time in milliseconds from audio stream start</summary>
    double? AudioEndMs = null,
    
    /// <summary>Confidence score 0.0-1.0</summary>
    float? Confidence = null,
    
    /// <summary>Word-level details with timestamps and speaker IDs</summary>
    List<WordDetail>? Words = null,
    
    /// <summary>Provider metadata</summary>
    EventMetadata? Metadata = null,
    
    /// <summary>Error details (only for error events)</summary>
    EventError? Error = null
);

public record WordDetail(
    string Word,
    double StartMs,
    double EndMs,
    float? Confidence = null,
    string? SpeakerId = null
);

public record EventMetadata(
    /// <summary>"deepgram" | "azure_speech"</summary>
    string Provider,
    string? Model = null,
    string? SessionId = null
);

public record EventError(
    string Code,
    string Message
);
```

---

### STEP 2: Implement DeepgramRealtimeTranscriber (PRIMARY)

**File**: `src/Services/DeepgramRealtimeTranscriber.cs`

**NuGet**: `dotnet add package Deepgram`

**Documentation**: https://developers.deepgram.com/docs/dotnet-sdk-streaming-transcription

```csharp
using Deepgram;
using Deepgram.Models.Listen.v2.WebSocket;
using Microsoft.Extensions.Logging;
using TeamsMediaBot.Models;
using System.Text.Json;

namespace TeamsMediaBot.Services;

/// <summary>
/// Real-time diarized transcription using Deepgram WebSocket API.
/// Implements streaming with speaker identification on every word.
/// 
/// Documentation: https://developers.deepgram.com/docs/dotnet-sdk-streaming-transcription
/// Diarization: https://developers.deepgram.com/docs/diarization
/// 
/// Last Grunted: 01/31/2026 12:00:00 PM PST
/// </summary>
public sealed class DeepgramRealtimeTranscriber : IRealtimeTranscriber
{
    private readonly string _apiKey;
    private readonly string _model;
    private readonly bool _diarize;
    private readonly PythonTranscriptPublisher _publisher;
    private readonly ILogger<DeepgramRealtimeTranscriber> _logger;
    
    private ListenWebSocketClient? _client;
    private string? _sessionId;
    private long _framesReceived;
    private long _bytesReceived;
    private double _audioOffsetMs; // Track cumulative audio position

    public DeepgramRealtimeTranscriber(
        string apiKey,
        string model,
        bool diarize,
        PythonTranscriptPublisher publisher,
        ILogger<DeepgramRealtimeTranscriber> logger)
    {
        _apiKey = apiKey;
        _model = model;
        _diarize = diarize;
        _publisher = publisher;
        _logger = logger;
    }

    public async Task StartAsync(CancellationToken ct = default)
    {
        if (_client != null)
        {
            _logger.LogWarning("Deepgram transcriber already started");
            return;
        }

        _logger.LogInformation("Starting Deepgram WebSocket connection (model={Model}, diarize={Diarize})", _model, _diarize);
        
        // Initialize Deepgram client
        var deepgramClient = ClientFactory.CreateListenWebSocketClient(_apiKey);
        _client = deepgramClient;

        // Configure live transcription options
        // Source: https://developers.deepgram.com/reference/speech-to-text-api/listen-streaming
        var options = new LiveSchema()
        {
            Model = _model,           // "nova-3" recommended for 2026
            Language = "en-US",
            Punctuate = true,
            SmartFormat = true,
            Diarize = _diarize,       // CRITICAL: Enables speaker detection
            InterimResults = true,    // Get partial results
            UtteranceEnd = "1000",    // End utterance after 1s silence
            Encoding = "linear16",    // PCM 16-bit
            SampleRate = 16000        // Teams sends 16kHz
        };

        // Subscribe to events
        _client.Subscribe(new EventHandler<OpenResponse>((sender, e) =>
        {
            _sessionId = e.RequestId ?? Guid.NewGuid().ToString();
            _logger.LogInformation("Deepgram session started: {SessionId}", _sessionId);
            PublishEvent("session_started", null);
        }));

        _client.Subscribe(new EventHandler<ResultResponse>((sender, e) =>
        {
            ProcessTranscriptionResult(e);
        }));

        _client.Subscribe(new EventHandler<CloseResponse>((sender, e) =>
        {
            _logger.LogInformation("Deepgram session closed");
            PublishEvent("session_stopped", null);
        }));

        _client.Subscribe(new EventHandler<ErrorResponse>((sender, e) =>
        {
            _logger.LogError("Deepgram error: {Error}", e.Message);
            PublishEvent("error", null, error: new EventError("DEEPGRAM_ERROR", e.Message));
        }));

        // Connect to Deepgram
        await _client.Connect(options);
        _logger.LogInformation("Deepgram WebSocket connected successfully");
    }

    /// <summary>
    /// Process Deepgram transcription result and normalize to TranscriptEvent.
    /// 
    /// Deepgram response format with diarization:
    /// - result.channel.alternatives[0].words[].speaker = 0, 1, 2...
    /// - We normalize to "speaker_0", "speaker_1", etc.
    /// 
    /// Source: https://developers.deepgram.com/docs/diarization#live-streaming
    /// </summary>
    private void ProcessTranscriptionResult(ResultResponse result)
    {
        if (result.Channel?.Alternatives == null || result.Channel.Alternatives.Count == 0)
            return;

        var alt = result.Channel.Alternatives[0];
        if (string.IsNullOrWhiteSpace(alt.Transcript))
            return;

        var isFinal = result.IsFinal ?? false;
        var eventType = isFinal ? "final" : "partial";

        // Extract speaker ID from first word (Deepgram assigns speaker per word)
        string? speakerId = null;
        if (_diarize && alt.Words != null && alt.Words.Count > 0)
        {
            var firstSpeaker = alt.Words[0].Speaker;
            speakerId = firstSpeaker.HasValue ? $"speaker_{firstSpeaker.Value}" : null;
        }

        // Build word-level details
        List<WordDetail>? words = null;
        if (alt.Words != null && alt.Words.Count > 0)
        {
            words = alt.Words.Select(w => new WordDetail(
                Word: w.Word ?? "",
                StartMs: (w.Start ?? 0) * 1000,
                EndMs: (w.End ?? 0) * 1000,
                Confidence: (float?)(w.Confidence ?? 0),
                SpeakerId: w.Speaker.HasValue ? $"speaker_{w.Speaker.Value}" : null
            )).ToList();
        }

        var audioStart = (result.Start ?? 0) * 1000;
        var audioEnd = audioStart + ((result.Duration ?? 0) * 1000);

        if (isFinal)
        {
            _logger.LogInformation("[FINAL] Speaker={Speaker}: {Text}", speakerId ?? "unknown", alt.Transcript);
        }
        else
        {
            _logger.LogDebug("[PARTIAL] Speaker={Speaker}: {Text}", speakerId ?? "unknown", alt.Transcript);
        }

        var evt = new TranscriptEvent(
            EventType: eventType,
            Text: alt.Transcript,
            TimestampUtc: DateTime.UtcNow.ToString("O"),
            SpeakerId: speakerId,
            AudioStartMs: audioStart,
            AudioEndMs: audioEnd,
            Confidence: (float?)(alt.Confidence ?? 0),
            Words: words,
            Metadata: new EventMetadata(
                Provider: "deepgram",
                Model: _model,
                SessionId: _sessionId
            )
        );

        _ = Task.Run(() => _publisher.PublishAsync(evt));
    }

    /// <summary>
    /// Push PCM audio frames to Deepgram WebSocket.
    /// Teams delivers 20ms frames at 16kHz/16-bit/mono = 640 bytes per frame.
    /// </summary>
    public void PushPcm16k16bitMono(ReadOnlySpan<byte> pcmFrame)
    {
        if (_client == null)
        {
            _logger.LogWarning("Cannot push audio - Deepgram not connected");
            return;
        }

        // Send raw PCM bytes to Deepgram
        _client.Send(pcmFrame.ToArray());

        _framesReceived++;
        _bytesReceived += pcmFrame.Length;
        _audioOffsetMs += 20; // 20ms per frame

        // Log stats every ~1 second (50 frames)
        if (_framesReceived % 50 == 0)
        {
            _logger.LogDebug("Audio stats: {Frames} frames, {Bytes} bytes ({Seconds:F1}s)",
                _framesReceived, _bytesReceived, _framesReceived * 0.02);
        }
    }

    public async Task StopAsync()
    {
        if (_client == null) return;

        _logger.LogInformation("Stopping Deepgram transcription...");

        try
        {
            // Send KeepAlive then Finalize as per Deepgram docs
            await _client.Finish();
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error stopping Deepgram connection");
        }

        _client = null;
        _logger.LogInformation("Deepgram stopped. Total: {Frames} frames, {Bytes} bytes", _framesReceived, _bytesReceived);
    }

    private void PublishEvent(string eventType, string? text, EventError? error = null)
    {
        var evt = new TranscriptEvent(
            EventType: eventType,
            Text: text,
            TimestampUtc: DateTime.UtcNow.ToString("O"),
            Metadata: new EventMetadata(Provider: "deepgram", Model: _model, SessionId: _sessionId),
            Error: error
        );
        _ = Task.Run(() => _publisher.PublishAsync(evt));
    }

    public async ValueTask DisposeAsync()
    {
        await StopAsync();
    }
}
```

---

### STEP 3: Implement AzureConversationTranscriber (FALLBACK)

**File**: `src/Services/AzureConversationTranscriber.cs`

**CRITICAL**: Uses `ConversationTranscriber` NOT `SpeechRecognizer`.

**Documentation**: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/get-started-stt-diarization

```csharp
using Microsoft.CognitiveServices.Speech;
using Microsoft.CognitiveServices.Speech.Audio;
using Microsoft.CognitiveServices.Speech.Transcription;
using Microsoft.Extensions.Logging;
using TeamsMediaBot.Models;
using System.Text.RegularExpressions;

namespace TeamsMediaBot.Services;

/// <summary>
/// Real-time diarized transcription using Azure ConversationTranscriber.
/// 
/// CRITICAL: This uses ConversationTranscriber, NOT SpeechRecognizer.
/// SpeechRecognizer does NOT support real-time diarization.
/// 
/// Speaker IDs: Azure returns "Guest-1", "Guest-2", etc.
/// We normalize to: "speaker_0", "speaker_1", etc.
/// 
/// Documentation: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/get-started-stt-diarization
/// GA Announcement: https://techcommunity.microsoft.com/blog/azure-ai-services-blog/announcing-general-availability-of-real-time-diarization/4147556
/// 
/// Last Grunted: 01/31/2026 12:00:00 PM PST
/// </summary>
public sealed class AzureConversationTranscriber : IRealtimeTranscriber
{
    private readonly string _speechKey;
    private readonly string _speechRegion;
    private readonly string _lang;
    private readonly string? _endpointId;
    private readonly PythonTranscriptPublisher _publisher;
    private readonly ILogger<AzureConversationTranscriber> _logger;

    private PushAudioInputStream? _push;
    private ConversationTranscriber? _transcriber;
    private string? _sessionId;
    private long _framesReceived;
    private long _bytesReceived;

    // Regex to extract speaker number from "Guest-1", "Guest-2", etc.
    private static readonly Regex SpeakerIdRegex = new(@"Guest-(\d+)", RegexOptions.Compiled);

    public AzureConversationTranscriber(
        string speechKey,
        string speechRegion,
        string language,
        string? endpointId,
        PythonTranscriptPublisher publisher,
        ILogger<AzureConversationTranscriber> logger)
    {
        _speechKey = speechKey;
        _speechRegion = speechRegion;
        _lang = language;
        _endpointId = string.IsNullOrWhiteSpace(endpointId) ? null : endpointId.Trim();
        _publisher = publisher;
        _logger = logger;
    }

    public async Task StartAsync(CancellationToken ct = default)
    {
        if (_transcriber != null)
        {
            _logger.LogWarning("Azure ConversationTranscriber already started");
            return;
        }

        _logger.LogInformation("Starting Azure ConversationTranscriber with real-time diarization...");

        // Configure audio input - Teams sends 16kHz/16-bit/mono PCM
        _push = AudioInputStream.CreatePushStream(AudioStreamFormat.GetWaveFormatPCM(16000, 16, 1));
        var audioConfig = AudioConfig.FromStreamInput(_push);

        // Configure speech
        var speechConfig = SpeechConfig.FromSubscription(_speechKey, _speechRegion);
        speechConfig.SpeechRecognitionLanguage = _lang;
        
        // CRITICAL: Enable intermediate diarization results
        // Source: Azure docs - PropertyId.SpeechServiceResponse_DiarizeIntermediateResults
        speechConfig.SetProperty(
            PropertyId.SpeechServiceResponse_DiarizeIntermediateResults, 
            "true"
        );

        if (_endpointId != null)
        {
            speechConfig.EndpointId = _endpointId;
        }

        // Use ConversationTranscriber (NOT SpeechRecognizer) for diarization
        _transcriber = new ConversationTranscriber(speechConfig, audioConfig);
        _sessionId = Guid.NewGuid().ToString();

        // Wire up events
        _transcriber.SessionStarted += (s, e) =>
        {
            _sessionId = e.SessionId;
            _logger.LogInformation("Azure session started: {SessionId}", _sessionId);
            PublishEvent("session_started", null, null);
        };

        _transcriber.SessionStopped += (s, e) =>
        {
            _logger.LogInformation("Azure session stopped");
            PublishEvent("session_stopped", null, null);
        };

        // Transcribing = partial/interim results (with speaker ID)
        _transcriber.Transcribing += (s, e) =>
        {
            if (!string.IsNullOrWhiteSpace(e.Result?.Text))
            {
                var speakerId = NormalizeSpeakerId(e.Result.SpeakerId);
                _logger.LogDebug("[PARTIAL] Speaker={Speaker}: {Text}", speakerId ?? "unknown", e.Result.Text);
                PublishEvent("partial", e.Result.Text, speakerId);
            }
        };

        // Transcribed = final results (with speaker ID)
        _transcriber.Transcribed += (s, e) =>
        {
            if (e.Result?.Reason == ResultReason.RecognizedSpeech && !string.IsNullOrWhiteSpace(e.Result.Text))
            {
                var speakerId = NormalizeSpeakerId(e.Result.SpeakerId);
                _logger.LogInformation("[FINAL] Speaker={Speaker}: {Text}", speakerId ?? "unknown", e.Result.Text);
                PublishEvent("final", e.Result.Text, speakerId);
            }
        };

        _transcriber.Canceled += (s, e) =>
        {
            _logger.LogError("Azure transcription canceled: {Reason} - {Details}", e.Reason, e.ErrorDetails);
            PublishEvent("error", null, null, new EventError(e.ErrorCode.ToString(), e.ErrorDetails ?? "Canceled"));
        };

        // Start continuous transcription
        await _transcriber.StartTranscribingAsync().ConfigureAwait(false);
        _logger.LogInformation("Azure ConversationTranscriber started successfully");
    }

    /// <summary>
    /// Normalize Azure speaker ID from "Guest-1" to "speaker_0".
    /// Azure uses 1-based indexing, we use 0-based for consistency with Deepgram.
    /// </summary>
    private static string? NormalizeSpeakerId(string? azureSpeakerId)
    {
        if (string.IsNullOrWhiteSpace(azureSpeakerId) || azureSpeakerId == "Unknown")
            return null;

        var match = SpeakerIdRegex.Match(azureSpeakerId);
        if (match.Success && int.TryParse(match.Groups[1].Value, out var num))
        {
            // Convert 1-based to 0-based
            return $"speaker_{num - 1}";
        }

        // Fallback: just use the raw ID as-is
        return azureSpeakerId.ToLowerInvariant().Replace("-", "_");
    }

    public void PushPcm16k16bitMono(ReadOnlySpan<byte> pcmFrame)
    {
        if (_push == null)
        {
            _logger.LogWarning("Cannot push audio - Azure transcriber not started");
            return;
        }

        _push.Write(pcmFrame.ToArray());

        _framesReceived++;
        _bytesReceived += pcmFrame.Length;

        if (_framesReceived % 50 == 0)
        {
            _logger.LogDebug("Audio stats: {Frames} frames, {Bytes} bytes ({Seconds:F1}s)",
                _framesReceived, _bytesReceived, _framesReceived * 0.02);
        }
    }

    public async Task StopAsync()
    {
        if (_transcriber == null) return;

        _logger.LogInformation("Stopping Azure ConversationTranscriber...");

        try
        {
            await _transcriber.StopTranscribingAsync().ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error stopping Azure transcriber");
        }

        _transcriber.Dispose();
        _transcriber = null;

        _push?.Close();
        _push = null;

        _logger.LogInformation("Azure stopped. Total: {Frames} frames, {Bytes} bytes", _framesReceived, _bytesReceived);
    }

    private void PublishEvent(string eventType, string? text, string? speakerId, EventError? error = null)
    {
        var evt = new TranscriptEvent(
            EventType: eventType,
            Text: text,
            TimestampUtc: DateTime.UtcNow.ToString("O"),
            SpeakerId: speakerId,
            Metadata: new EventMetadata(Provider: "azure_speech", Model: null, SessionId: _sessionId),
            Error: error
        );
        _ = Task.Run(() => _publisher.PublishAsync(evt));
    }

    public async ValueTask DisposeAsync()
    {
        await StopAsync();
    }
}
```

---

### STEP 4: Update BotConfiguration for Provider Selection

**File**: `src/Models/BotConfiguration.cs`

Add Deepgram configuration:

```csharp
namespace TeamsMediaBot.Models;

public class BotConfiguration
{
    public required string TenantId { get; set; }
    public required string AppId { get; set; }
    public required string AppSecret { get; set; }
    public required string NotificationUrl { get; set; }
    public required string LocalHttpListenUrl { get; set; }
    public int LocalHttpListenPort { get; set; } = 9443;
}

public class SttConfiguration
{
    /// <summary>"Deepgram" (recommended) or "AzureSpeech"</summary>
    public string Provider { get; set; } = "Deepgram";
    
    public DeepgramConfiguration? Deepgram { get; set; }
    public AzureSpeechConfiguration? AzureSpeech { get; set; }
}

public class DeepgramConfiguration
{
    public required string ApiKey { get; set; }
    
    /// <summary>Model to use. Recommended: "nova-3" (2025/2026)</summary>
    public string Model { get; set; } = "nova-3";
    
    /// <summary>Enable speaker diarization. MUST be true for this use case.</summary>
    public bool Diarize { get; set; } = true;
}

public class AzureSpeechConfiguration
{
    public required string Key { get; set; }
    public required string Region { get; set; }
    public string RecognitionLanguage { get; set; } = "en-US";
    public string? EndpointId { get; set; }
}
```

---

### STEP 5: Update TranscriberFactory

**File**: `src/Services/AzureSpeechRealtimeTranscriber.cs`

Replace `TranscriberFactory` to support both providers:

```csharp
public class TranscriberFactory
{
    private readonly SttConfiguration _stt;
    private readonly string _pythonEndpoint;
    private readonly ILoggerFactory _loggerFactory;

    public TranscriberFactory(
        SttConfiguration stt,
        string pythonEndpoint,
        ILoggerFactory loggerFactory)
    {
        _stt = stt;
        _pythonEndpoint = pythonEndpoint;
        _loggerFactory = loggerFactory;
    }

    public IRealtimeTranscriber Create()
    {
        var publisherLogger = _loggerFactory.CreateLogger<PythonTranscriptPublisher>();
        var publisher = new PythonTranscriptPublisher(_pythonEndpoint, publisherLogger);
        
        var provider = (_stt.Provider ?? "Deepgram").Trim();

        // PRIMARY: Deepgram (best diarization)
        if (provider.Equals("Deepgram", StringComparison.OrdinalIgnoreCase))
        {
            var cfg = _stt.Deepgram ?? throw new InvalidOperationException(
                "STT provider 'Deepgram' selected but Stt.Deepgram config is missing.");

            var logger = _loggerFactory.CreateLogger<DeepgramRealtimeTranscriber>();
            return new DeepgramRealtimeTranscriber(
                cfg.ApiKey,
                cfg.Model,
                cfg.Diarize,
                publisher,
                logger);
        }

        // FALLBACK: Azure Speech ConversationTranscriber
        if (provider.Equals("AzureSpeech", StringComparison.OrdinalIgnoreCase) ||
            provider.Equals("Azure", StringComparison.OrdinalIgnoreCase))
        {
            var cfg = _stt.AzureSpeech ?? throw new InvalidOperationException(
                "STT provider 'AzureSpeech' selected but Stt.AzureSpeech config is missing.");

            var logger = _loggerFactory.CreateLogger<AzureConversationTranscriber>();
            return new AzureConversationTranscriber(
                cfg.Key,
                cfg.Region,
                cfg.RecognitionLanguage,
                cfg.EndpointId,
                publisher,
                logger);
        }

        throw new NotSupportedException(
            $"STT provider '{provider}' is not supported. Use 'Deepgram' or 'AzureSpeech'.");
    }
}
```

---

### STEP 6: Update PythonTranscriptPublisher

**File**: `src/Services/PythonTranscriptPublisher.cs`

Update to serialize the new TranscriptEvent format:

```csharp
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Publishes transcript events to Python FastAPI endpoint.
/// Uses snake_case JSON for Python compatibility.
/// 
/// Last Grunted: 01/31/2026 12:00:00 PM PST
/// </summary>
public class PythonTranscriptPublisher
{
    private readonly string _endpoint;
    private readonly ILogger<PythonTranscriptPublisher> _logger;
    private readonly HttpClient _http;
    private readonly JsonSerializerOptions _jsonOptions;

    public PythonTranscriptPublisher(string endpoint, ILogger<PythonTranscriptPublisher> logger)
    {
        _endpoint = endpoint;
        _logger = logger;
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
        
        // Use snake_case for Python compatibility
        _jsonOptions = new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
            DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
        };
    }

    public async Task PublishAsync(TranscriptEvent evt)
    {
        try
        {
            var response = await _http.PostAsJsonAsync(_endpoint, evt, _jsonOptions);
            
            if (!response.IsSuccessStatusCode)
            {
                _logger.LogWarning("Python endpoint returned {Status}", response.StatusCode);
            }
        }
        catch (TaskCanceledException)
        {
            _logger.LogWarning("Timeout publishing to Python endpoint");
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to publish to Python endpoint");
        }
    }
}
```

---

### ~~STEP 7: Update Python Transcript Sink~~ ✅ COMPLETE

**Status**: IMPLEMENTED and TESTED (2026-02-01)

The Python transcript sink at `python/transcript_sink.py` **exceeds** the original specification:
- Session management endpoints (`/session/start`, `/session/end`, `/session/map-speaker`)
- v1/v2 format normalization (backward compatible with legacy C# bot format)
- Interview agent integration (`interview_agent/` package)
- Comprehensive test suite (67 tests pass)
- File-based transcript persistence

**To test locally:**
```bash
cd python
uv sync
uv run pytest tests/ -v  # Run tests
uv run python transcript_sink.py  # Start server
```

---

### STEP 8: Update appsettings.json

**File**: `src/Config/appsettings.json`

```json
{
  "Bot": {
    "TenantId": "YOUR_TENANT_ID",
    "AppId": "YOUR_APP_ID",
    "AppSecret": "YOUR_APP_SECRET",
    "NotificationUrl": "https://teamsbot.qmachina.com/api/calling",
    "LocalHttpListenUrl": "https://0.0.0.0:9443",
    "LocalHttpListenPort": 9443
  },
  "Stt": {
    "Provider": "Deepgram",
    "Deepgram": {
      "ApiKey": "YOUR_DEEPGRAM_API_KEY",
      "Model": "nova-3",
      "Diarize": true
    },
    "AzureSpeech": {
      "Key": "YOUR_AZURE_SPEECH_KEY",
      "Region": "eastus",
      "RecognitionLanguage": "en-US",
      "EndpointId": null
    }
  },
  "MediaPlatformSettings": {
    "ApplicationId": "YOUR_APP_ID",
    "CertificateThumbprint": "YOUR_CERT_THUMBPRINT",
    "InstanceInternalPort": 8445,
    "InstancePublicPort": 8445,
    "ServiceFqdn": "media.qmachina.com",
    "InstancePublicIPAddress": "0.0.0.0"
  },
  "TranscriptSink": {
    "PythonEndpoint": "http://127.0.0.1:8765/transcript"
  }
}
```

---

### STEP 9: Add Deepgram NuGet Package

Run in `src/` directory:

```bash
dotnet add package Deepgram
```

---

### ~~STEP 10: Update requirements.txt~~ ✅ COMPLETE

**Status**: IMPLEMENTED via `pyproject.toml` (better than requirements.txt)

Current dependencies in `python/pyproject.toml`:
```toml
dependencies = [
    "fastapi>=0.115.0",
    "httpx>=0.28.0",
    "uvicorn[standard]>=0.32.0",
    "openai-agents>=0.0.16",
    "pydantic>=2.0.0",
]

[tool.uv]
dev-dependencies = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.24.0",
]
```

---

## VERIFICATION CHECKLIST

After implementation, verify:

### C# Bot (Requires Windows/.NET)
- [ ] `dotnet build --configuration Release` succeeds
- [ ] No linter errors in new files
- [ ] TranscriptEvent serializes to snake_case JSON

### Python Sink ✅ VERIFIED (2026-02-01)
- [x] `uv run transcript_sink.py` starts without errors
- [x] Health check returns 200: `curl http://127.0.0.1:8765/health`
- [x] 67/67 unit tests pass: `uv run pytest tests/ -v`
- [x] Session management works (start/map-speaker/end)
- [x] Agent integration works (graceful degradation without API key)

### End-to-End (Requires deployed C# bot + Teams meeting)
- [ ] Join a test meeting with bot
- [ ] Verify speaker IDs appear in Python logs: `[FINAL] speaker_0: Hello`
- [ ] Verify stats show multiple speakers: `curl http://127.0.0.1:8765/stats`

---

## SMOKE TEST COMMANDS

### Test v2 Event Format (Deepgram style)

```bash
curl -X POST http://127.0.0.1:8765/transcript \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "final",
    "text": "Hello, this is the first speaker.",
    "timestamp_utc": "2026-01-31T12:34:56.789Z",
    "speaker_id": "speaker_0",
    "audio_start_ms": 0.0,
    "audio_end_ms": 2500.0,
    "confidence": 0.95,
    "metadata": {"provider": "deepgram", "model": "nova-3"}
  }'
```

### Test Speaker Change

```bash
curl -X POST http://127.0.0.1:8765/transcript \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "final",
    "text": "And this is the second speaker responding.",
    "timestamp_utc": "2026-01-31T12:34:59.123Z",
    "speaker_id": "speaker_1",
    "confidence": 0.92,
    "metadata": {"provider": "deepgram", "model": "nova-3"}
  }'
```

### Verify Stats

```bash
curl http://127.0.0.1:8765/stats
# Expected: speakers_detected: ["speaker_0", "speaker_1"]
```

---

## FILE CHANGES SUMMARY

| Action | File | Description | Status |
|--------|------|-------------|--------|
| REPLACE | `src/Models/TranscriptEvent.cs` | Add diarization fields | ⏳ PENDING |
| CREATE | `src/Services/DeepgramRealtimeTranscriber.cs` | Primary STT provider | ⏳ PENDING |
| CREATE | `src/Services/AzureConversationTranscriber.cs` | Fallback STT (replaces old SpeechRecognizer approach) | ⏳ PENDING |
| UPDATE | `src/Models/BotConfiguration.cs` | Add Deepgram config section | ⏳ PENDING |
| UPDATE | `src/Services/AzureSpeechRealtimeTranscriber.cs` | Update TranscriberFactory only | ⏳ PENDING |
| UPDATE | `src/Services/PythonTranscriptPublisher.cs` | Snake_case JSON serialization | ⏳ PENDING |
| REPLACE | `python/transcript_sink.py` | Diarization-aware receiver | ✅ COMPLETE |
| UPDATE | `src/Config/appsettings.json` | Add Deepgram config | ⏳ PENDING |
| UPDATE | `python/pyproject.toml` | Dependencies (replaced requirements.txt) | ✅ COMPLETE |

---

## AUTHORITATIVE DOCUMENTATION SOURCES

All implementations verified against 2025/2026 documentation:

| Provider | Documentation URL | Last Verified |
|----------|-------------------|---------------|
| Deepgram Streaming | https://developers.deepgram.com/docs/dotnet-sdk-streaming-transcription | 2026-01-31 |
| Deepgram Diarization | https://developers.deepgram.com/docs/diarization | 2026-01-31 |
| Azure ConversationTranscriber | https://learn.microsoft.com/en-us/azure/ai-services/speech-service/get-started-stt-diarization | 2026-01-31 |
| Azure Real-Time Diarization GA | https://techcommunity.microsoft.com/blog/azure-ai-services-blog/announcing-general-availability-of-real-time-diarization/4147556 | May 2024 |
| Teams Bot Media SDK | https://microsoftgraph.github.io/microsoft-graph-comms-samples/docs/bot_media/index.html | 2026-01-31 |

---

## NEXT STEPS (Post-MVP)

1. **Streamlit UI** - Build separate UI for agent interaction
2. **Speaker Naming** - Map `speaker_0` to actual participant names via Graph API
3. **Meeting Context** - Include meeting title, attendees in transcript metadata
4. **Multi-Language** - Add language detection and multilingual support
