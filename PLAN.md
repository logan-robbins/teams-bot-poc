# IMPLEMENTATION GUIDE: Teams Meeting → Diarized Transcription → Python Agent

**Version**: 2.0 (2026-01-31)  
**Status**: Ready for AI/LLM Coding Agent Implementation  
**Documentation Sources**: All implementations MUST use 2025/2026 documentation only

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

### STEP 7: Update Python Transcript Sink

**File**: `python/transcript_sink.py`

Replace with diarization-aware version:

```python
"""
Python Transcript Receiver with Diarization Support

Receives real-time diarized transcript events from C# bot.
Filters to final events with speaker IDs for agent processing.

Last Grunted: 01/31/2026 12:00:00 PM PST
"""
from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import Optional, List
import uvicorn
import asyncio
from datetime import datetime
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Teams Transcript Sink", version="2.0.0")


class WordDetail(BaseModel):
    word: str
    start_ms: float
    end_ms: float
    confidence: Optional[float] = None
    speaker_id: Optional[str] = None


class EventMetadata(BaseModel):
    provider: str
    model: Optional[str] = None
    session_id: Optional[str] = None


class EventError(BaseModel):
    code: str
    message: str


class TranscriptEvent(BaseModel):
    """Provider-agnostic transcript event with diarization support."""
    event_type: str  # "partial" | "final" | "session_started" | "session_stopped" | "error"
    text: Optional[str] = None
    timestamp_utc: str
    speaker_id: Optional[str] = None  # "speaker_0", "speaker_1", etc.
    audio_start_ms: Optional[float] = None
    audio_end_ms: Optional[float] = None
    confidence: Optional[float] = None
    words: Optional[List[WordDetail]] = None
    metadata: Optional[EventMetadata] = None
    error: Optional[EventError] = None


# Async queue for agent consumption
transcript_queue: asyncio.Queue[TranscriptEvent] = asyncio.Queue()

# Stats tracking
stats = {
    "events_received": 0,
    "partial_transcripts": 0,
    "final_transcripts": 0,
    "speakers_detected": set(),
    "errors": 0,
    "sessions": 0,
    "started_at": datetime.utcnow().isoformat()
}


@app.post("/transcript")
async def receive_transcript(evt: TranscriptEvent):
    """
    Receive transcript events from C# bot.
    
    Event format (v2 with diarization):
    {
        "event_type": "final",
        "text": "Hello everyone",
        "timestamp_utc": "2026-01-31T12:34:56.789Z",
        "speaker_id": "speaker_0",
        "audio_start_ms": 1000.0,
        "audio_end_ms": 2500.0,
        "confidence": 0.95,
        "metadata": {"provider": "deepgram", "model": "nova-3"}
    }
    """
    stats["events_received"] += 1
    
    if evt.event_type == "partial":
        stats["partial_transcripts"] += 1
        if evt.speaker_id:
            logger.debug(f"[PARTIAL] {evt.speaker_id}: {evt.text}")
        else:
            logger.debug(f"[PARTIAL] {evt.text}")
            
    elif evt.event_type == "final":
        stats["final_transcripts"] += 1
        if evt.speaker_id:
            stats["speakers_detected"].add(evt.speaker_id)
            logger.info(f"[FINAL] {evt.speaker_id}: {evt.text}")
        else:
            logger.info(f"[FINAL] (no speaker): {evt.text}")
        
        # Push to agent queue for processing
        await transcript_queue.put(evt)
        
    elif evt.event_type == "session_started":
        stats["sessions"] += 1
        stats["speakers_detected"] = set()  # Reset for new session
        logger.info(f"Session started (provider: {evt.metadata.provider if evt.metadata else 'unknown'})")
        
    elif evt.event_type == "session_stopped":
        logger.info(f"Session stopped. Speakers detected: {stats['speakers_detected']}")
        
    elif evt.event_type == "error":
        stats["errors"] += 1
        logger.error(f"Transcription error: {evt.error}")
    
    return {"ok": True, "received_at": datetime.utcnow().isoformat()}


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "Teams Transcript Sink v2",
        "timestamp": datetime.utcnow().isoformat()
    }


@app.get("/stats")
async def get_stats():
    return {
        "stats": {
            **stats,
            "speakers_detected": list(stats["speakers_detected"])
        },
        "queue_size": transcript_queue.qsize()
    }


async def agent_processing_loop():
    """
    Background task that processes FINAL transcript events with speaker IDs.
    
    This is the integration point for your agentic framework.
    Only final transcripts (not partial) are processed to reduce noise.
    
    Example integrations:
    - LangGraph agent with speaker context
    - Custom agent with speaker-aware memory
    - Real-time meeting summarizer
    """
    logger.info("Agent processing loop started")
    
    while True:
        try:
            evt: TranscriptEvent = await transcript_queue.get()
            
            # Only process final transcripts with text
            if evt.event_type == "final" and evt.text:
                speaker = evt.speaker_id or "unknown"
                text = evt.text
                
                logger.info(f"AGENT_INPUT | {speaker}: {text}")
                
                # ============================================
                # YOUR AGENT INTEGRATION HERE
                # ============================================
                # Examples:
                #
                # 1. LangGraph with speaker context:
                # await agent.invoke({
                #     "speaker": speaker,
                #     "message": text,
                #     "timestamp": evt.timestamp_utc
                # })
                #
                # 2. Custom agent with memory:
                # await agent.process(speaker, text)
                #
                # 3. Forward to MCP server:
                # await mcp_client.send_transcript(speaker, text)
                #
                # 4. Store in vector DB with speaker metadata:
                # await vectordb.add(text, metadata={"speaker": speaker})
                # ============================================
                
                pass
                
        except Exception as e:
            logger.error(f"Error in agent processing loop: {e}", exc_info=True)
            await asyncio.sleep(1)


@app.on_event("startup")
async def startup_event():
    """Start background agent processing loop."""
    asyncio.create_task(agent_processing_loop())


if __name__ == "__main__":
    logger.info("Starting Teams Transcript Sink v2 on http://0.0.0.0:8765")
    logger.info("Endpoints:")
    logger.info("  POST /transcript - Receive transcript events")
    logger.info("  GET  /health    - Health check")
    logger.info("  GET  /stats     - Statistics")
    
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")
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

### STEP 10: Update requirements.txt

**File**: `python/requirements.txt`

```
fastapi>=0.109.0
uvicorn>=0.27.0
pydantic>=2.5.0
```

---

## VERIFICATION CHECKLIST

After implementation, verify:

### C# Bot
- [ ] `dotnet build --configuration Release` succeeds
- [ ] No linter errors in new files
- [ ] TranscriptEvent serializes to snake_case JSON

### Python Sink
- [ ] `uv run transcript_sink.py` starts without errors
- [ ] Health check returns 200: `curl http://127.0.0.1:8765/health`

### End-to-End
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

| Action | File | Description |
|--------|------|-------------|
| REPLACE | `src/Models/TranscriptEvent.cs` | Add diarization fields |
| CREATE | `src/Services/DeepgramRealtimeTranscriber.cs` | Primary STT provider |
| CREATE | `src/Services/AzureConversationTranscriber.cs` | Fallback STT (replaces old SpeechRecognizer approach) |
| UPDATE | `src/Models/BotConfiguration.cs` | Add Deepgram config section |
| UPDATE | `src/Services/AzureSpeechRealtimeTranscriber.cs` | Update TranscriberFactory only |
| UPDATE | `src/Services/PythonTranscriptPublisher.cs` | Snake_case JSON serialization |
| REPLACE | `python/transcript_sink.py` | Diarization-aware receiver |
| UPDATE | `src/Config/appsettings.json` | Add Deepgram config |
| UPDATE | `python/requirements.txt` | Add pydantic |

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
