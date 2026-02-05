using Deepgram;
using Deepgram.Clients.Interfaces.v2;
using Deepgram.Models.Listen.v2.WebSocket;
using Microsoft.Extensions.Logging;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Real-time diarized transcription using Deepgram WebSocket API.
/// Implements streaming with speaker identification on every word.
/// </summary>
/// <remarks>
/// <para>
/// This transcriber uses the Deepgram SDK v6+ ListenWebSocketClient for streaming audio.
/// Audio is pushed as raw PCM 16kHz/16-bit/mono frames from Teams media SDK.
/// </para>
/// <para>
/// Documentation: https://developers.deepgram.com/docs/dotnet-sdk-streaming-transcription
/// Diarization: https://developers.deepgram.com/docs/diarization
/// </para>
/// </remarks>
public sealed class DeepgramRealtimeTranscriber : IRealtimeTranscriber
{
    private readonly string _apiKey;
    private readonly string _model;
    private readonly bool _diarize;
    private readonly PythonTranscriptPublisher _publisher;
    private readonly ILogger<DeepgramRealtimeTranscriber> _logger;
    
    private IListenWebSocketClient? _client;
    private string? _sessionId;
    private long _framesReceived;
    private long _bytesReceived;
    private bool _isDisposed;

    /// <summary>
    /// Initializes a new instance of the <see cref="DeepgramRealtimeTranscriber"/> class.
    /// </summary>
    /// <param name="apiKey">The Deepgram API key.</param>
    /// <param name="model">The Deepgram model to use (e.g., "nova-3").</param>
    /// <param name="diarize">Whether to enable speaker diarization.</param>
    /// <param name="publisher">The publisher for sending transcript events to Python.</param>
    /// <param name="logger">The logger instance.</param>
    /// <exception cref="ArgumentNullException">Thrown when required parameters are null.</exception>
    public DeepgramRealtimeTranscriber(
        string apiKey,
        string model,
        bool diarize,
        PythonTranscriptPublisher publisher,
        ILogger<DeepgramRealtimeTranscriber> logger)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(apiKey);
        ArgumentException.ThrowIfNullOrWhiteSpace(model);
        ArgumentNullException.ThrowIfNull(publisher);
        ArgumentNullException.ThrowIfNull(logger);
        
        _apiKey = apiKey;
        _model = model;
        _diarize = diarize;
        _publisher = publisher;
        _logger = logger;
    }

    /// <inheritdoc/>
    public async Task StartAsync(CancellationToken ct = default)
    {
        ObjectDisposedException.ThrowIf(_isDisposed, this);
        
        if (_client != null)
        {
            _logger.LogWarning("Deepgram transcriber already started");
            return;
        }

        _logger.LogInformation(
            "Starting Deepgram WebSocket connection (Model={Model}, Diarize={Diarize})",
            _model, _diarize);
        
        // Initialize Deepgram client using factory pattern
        // Per Deepgram SDK docs: https://developers.deepgram.com/docs/dotnet-sdk-streaming-transcription
        var deepgramClient = ClientFactory.CreateListenWebSocketClient(_apiKey);
        _client = deepgramClient;

        // Configure live transcription options
        // Source: https://developers.deepgram.com/reference/speech-to-text-api/listen-streaming
        var options = new LiveSchema
        {
            Model = _model,           // "nova-3" recommended for 2025/2026
            Language = "en",
            Punctuate = true,
            SmartFormat = true,
            Diarize = _diarize,       // CRITICAL: Enables speaker detection per word
            InterimResults = true,    // Get partial results for real-time UX
            EndPointing = "10",       // Fast utterance endpointing (10ms silence) for quick speaker turns
            UtteranceEnd = "1000",    // Fire utterance_end event after 1s silence
            Encoding = "linear16",    // PCM 16-bit signed little-endian
            SampleRate = 16000        // Teams Media SDK sends 16kHz
        };

        // Subscribe to events using Deepgram's event-driven pattern
        // Discard return values: Subscribe registers handlers and does not need to be awaited
        _ = _client.Subscribe(new EventHandler<OpenResponse>(OnConnectionOpened));
        _ = _client.Subscribe(new EventHandler<ResultResponse>(OnTranscriptionResult));
        _ = _client.Subscribe(new EventHandler<CloseResponse>(OnConnectionClosed));
        _ = _client.Subscribe(new EventHandler<ErrorResponse>(OnError));

        // Connect to Deepgram WebSocket endpoint
        await _client.Connect(options).ConfigureAwait(false);
        _logger.LogInformation("Deepgram WebSocket connected successfully");
    }

    /// <summary>
    /// Handles the WebSocket connection opened event.
    /// </summary>
    private void OnConnectionOpened(object? sender, OpenResponse e)
    {
        _sessionId = Guid.NewGuid().ToString("N");
        _logger.LogInformation("Deepgram session started: {SessionId}", _sessionId);
        PublishEventAsync("session_started", text: null);
    }

    /// <summary>
    /// Handles transcription result events.
    /// </summary>
    private void OnTranscriptionResult(object? sender, ResultResponse e)
    {
        ProcessTranscriptionResult(e);
    }

    /// <summary>
    /// Handles the WebSocket connection closed event.
    /// </summary>
    private void OnConnectionClosed(object? sender, CloseResponse e)
    {
        _logger.LogInformation("Deepgram session closed");
        PublishEventAsync("session_stopped", text: null);
    }

    /// <summary>
    /// Handles error events from the Deepgram WebSocket.
    /// </summary>
    private void OnError(object? sender, ErrorResponse e)
    {
        _logger.LogError("Deepgram error: {Error}", e.Message);
        PublishEventAsync("error", text: null, error: new EventError("DEEPGRAM_ERROR", e.Message ?? "Unknown error"));
    }

    /// <summary>
    /// Processes a Deepgram transcription result and normalizes it to a <see cref="TranscriptEvent"/>.
    /// </summary>
    /// <remarks>
    /// Deepgram response format with diarization:
    /// <list type="bullet">
    ///   <item>result.channel.alternatives[0].words[].speaker = 0, 1, 2...</item>
    ///   <item>We normalize speaker IDs to "speaker_0", "speaker_1", etc.</item>
    /// </list>
    /// Source: https://developers.deepgram.com/docs/diarization#live-streaming
    /// </remarks>
    private void ProcessTranscriptionResult(ResultResponse result)
    {
        if (result.Channel?.Alternatives is not { Count: > 0 })
        {
            return;
        }

        var alternative = result.Channel.Alternatives[0];
        if (string.IsNullOrWhiteSpace(alternative.Transcript))
        {
            return;
        }

        var isFinal = result.IsFinal ?? false;
        var eventType = isFinal ? "final" : "partial";

        // Extract speaker ID from first word (Deepgram assigns speaker per word)
        string? speakerId = null;
        if (_diarize && alternative.Words is { Count: > 0 })
        {
            var firstSpeaker = alternative.Words[0].Speaker;
            speakerId = firstSpeaker.HasValue ? $"speaker_{firstSpeaker.Value}" : null;
        }

        // Build word-level details with timing and speaker attribution
        List<WordDetail>? words = null;
        if (alternative.Words is { Count: > 0 })
        {
            words = alternative.Words.Select(w => new WordDetail(
                Word: w.PunctuatedWord ?? w.HeardWord ?? string.Empty,
                StartMs: (double)(w.Start ?? 0m) * 1000,
                EndMs: (double)(w.End ?? 0m) * 1000,
                Confidence: (float?)(w.Confidence ?? 0),
                SpeakerId: w.Speaker.HasValue ? $"speaker_{w.Speaker.Value}" : null
            )).ToList();
        }

        var audioStartMs = (double)(result.Start ?? 0m) * 1000;
        var audioEndMs = audioStartMs + ((double)(result.Duration ?? 0m) * 1000);

        // Log transcription results with structured logging
        if (isFinal)
        {
            _logger.LogInformation(
                "[FINAL] Speaker={SpeakerId}: {Text}",
                speakerId ?? "unknown",
                alternative.Transcript);
        }
        else
        {
            _logger.LogDebug(
                "[PARTIAL] Speaker={SpeakerId}: {Text}",
                speakerId ?? "unknown",
                alternative.Transcript);
        }

        var transcriptEvent = new TranscriptEvent(
            EventType: eventType,
            Text: alternative.Transcript,
            TimestampUtc: DateTime.UtcNow.ToString("O"),
            SpeakerId: speakerId,
            AudioStartMs: audioStartMs,
            AudioEndMs: audioEndMs,
            Confidence: (float?)(alternative.Confidence ?? 0),
            Words: words,
            Metadata: new EventMetadata(
                Provider: "deepgram",
                Model: _model,
                SessionId: _sessionId
            )
        );

        // Fire-and-forget publish (don't block audio processing thread)
        PublishEventAsync(transcriptEvent);
    }

    /// <inheritdoc/>
    /// <remarks>
    /// Teams Media SDK delivers 20ms frames at 16kHz/16-bit/mono = 640 bytes per frame.
    /// This method is called from the audio receive callback and must not block.
    /// </remarks>
    public void PushPcm16k16bitMono(ReadOnlySpan<byte> pcmFrame)
    {
        if (_isDisposed || _client is null)
        {
            _logger.LogWarning("Cannot push audio - Deepgram not connected or disposed");
            return;
        }

        // Send raw PCM bytes to Deepgram WebSocket
        // Note: ToArray() is required as the SDK doesn't accept Span<T>
        _client.Send(pcmFrame.ToArray());

        _framesReceived++;
        _bytesReceived += pcmFrame.Length;

        // Log stats every ~1 second (50 frames at 20ms each)
        if (_framesReceived % 50 == 0)
        {
            _logger.LogDebug(
                "Audio stats: Frames={FrameCount}, Bytes={ByteCount}, Duration={DurationSeconds:F1}s",
                _framesReceived,
                _bytesReceived,
                _framesReceived * 0.02);
        }
    }

    /// <inheritdoc/>
    public async Task StopAsync()
    {
        if (_client is null)
        {
            return;
        }

        _logger.LogInformation("Stopping Deepgram transcription...");

        try
        {
            // Send finalize to flush remaining audio, then stop the connection
            await _client.SendFinalize().ConfigureAwait(false);
            await _client.Stop(new CancellationTokenSource(TimeSpan.FromSeconds(5))).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error stopping Deepgram connection");
        }

        _client = null;
        _logger.LogInformation(
            "Deepgram stopped. Total: Frames={FrameCount}, Bytes={ByteCount}",
            _framesReceived,
            _bytesReceived);
    }

    /// <summary>
    /// Publishes a transcript event asynchronously without blocking.
    /// </summary>
    private void PublishEventAsync(string eventType, string? text, EventError? error = null)
    {
        var transcriptEvent = new TranscriptEvent(
            EventType: eventType,
            Text: text,
            TimestampUtc: DateTime.UtcNow.ToString("O"),
            Metadata: new EventMetadata(Provider: "deepgram", Model: _model, SessionId: _sessionId),
            Error: error
        );
        PublishEventAsync(transcriptEvent);
    }

    /// <summary>
    /// Publishes a transcript event asynchronously without blocking.
    /// </summary>
    private void PublishEventAsync(TranscriptEvent transcriptEvent)
    {
        // Fire-and-forget: don't block the caller (typically audio processing thread)
        _ = Task.Run(async () =>
        {
            try
            {
                await _publisher.PublishAsync(transcriptEvent).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Failed to publish transcript event");
            }
        });
    }

    /// <inheritdoc/>
    public async ValueTask DisposeAsync()
    {
        if (_isDisposed)
        {
            return;
        }

        _isDisposed = true;
        await StopAsync().ConfigureAwait(false);
        GC.SuppressFinalize(this);
    }
}
