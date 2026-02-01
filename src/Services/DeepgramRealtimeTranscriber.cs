using Deepgram;
using Deepgram.Models.Listen.v2.WebSocket;
using Microsoft.Extensions.Logging;
using TeamsMediaBot.Models;

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
