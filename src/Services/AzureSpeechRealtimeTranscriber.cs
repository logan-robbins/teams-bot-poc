using Microsoft.CognitiveServices.Speech;
using Microsoft.CognitiveServices.Speech.Audio;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Real-time speech transcription using Azure Speech SDK
/// Implements streaming PCM audio → continuous recognition → transcript events
/// Based on Part I (I3) and Part B (B2) of the validated guide
/// Sources: S15 (audio streams), S16 (PCM format), S17-S18 (continuous recognition)
/// </summary>
public sealed class AzureSpeechRealtimeTranscriber : IAsyncDisposable
{
    private readonly string _speechKey;
    private readonly string _speechRegion;
    private readonly string _lang;
    private readonly PythonTranscriptPublisher _publisher;
    private readonly ILogger<AzureSpeechRealtimeTranscriber> _logger;

    private PushAudioInputStream? _push;
    private SpeechRecognizer? _recognizer;
    private long _framesReceived;
    private long _bytesReceived;

    public AzureSpeechRealtimeTranscriber(
        string speechKey,
        string speechRegion,
        string language,
        PythonTranscriptPublisher publisher,
        ILogger<AzureSpeechRealtimeTranscriber> logger)
    {
        _speechKey = speechKey;
        _speechRegion = speechRegion;
        _lang = language;
        _publisher = publisher;
        _logger = logger;
    }

    /// <summary>
    /// Start continuous speech recognition
    /// Per S17: StartContinuousRecognitionAsync begins recognition that continues until stopped
    /// </summary>
    public async Task StartAsync(CancellationToken ct = default)
    {
        if (_recognizer != null)
        {
            _logger.LogWarning("Transcriber already started");
            return;
        }

        _logger.LogInformation("Starting Azure Speech continuous recognition...");

        // Teams audio is 16 kHz / 16-bit / mono PCM per S5
        // PushAudioInputStream defaults to 16 kHz / 16-bit / mono PCM per S16
        // These match perfectly - no conversion needed
        _push = AudioInputStream.CreatePushStream();
        var audio = AudioConfig.FromStreamInput(_push);

        var cfg = SpeechConfig.FromSubscription(_speechKey, _speechRegion);
        cfg.SpeechRecognitionLanguage = _lang;

        _recognizer = new SpeechRecognizer(cfg, audio);

        // Wire up event handlers per S18
        _recognizer.SessionStarted += (_, __) =>
        {
            _logger.LogInformation("Speech recognition session started");
            FireAndForget("session_started", null);
        };

        _recognizer.SessionStopped += (_, __) =>
        {
            _logger.LogInformation("Speech recognition session stopped");
            FireAndForget("session_stopped", null);
        };

        // Recognizing = interim/partial results
        _recognizer.Recognizing += (_, e) =>
        {
            if (!string.IsNullOrWhiteSpace(e.Result?.Text))
            {
                _logger.LogDebug("Recognizing (partial): {Text}", e.Result.Text);
                FireAndForget("recognizing", e.Result.Text);
            }
        };

        // Recognized = final results
        _recognizer.Recognized += (_, e) =>
        {
            if (e.Result?.Reason == ResultReason.RecognizedSpeech && !string.IsNullOrWhiteSpace(e.Result.Text))
            {
                _logger.LogInformation("Recognized (final): {Text}", e.Result.Text);
                FireAndForget("recognized", e.Result.Text);
            }
        };

        // Canceled = errors
        _recognizer.Canceled += (_, e) =>
        {
            _logger.LogError("Speech recognition canceled: {Reason} - {ErrorDetails}", e.Reason, e.ErrorDetails);
            FireAndForget("canceled", null, $"{e.Reason}: {e.ErrorDetails}");
        };

        await _recognizer.StartContinuousRecognitionAsync().ConfigureAwait(false);
        _logger.LogInformation("Speech recognition started successfully");
    }

    /// <summary>
    /// Push PCM audio frames into the recognizer
    /// Per S5: Teams delivers 20ms frames (640 bytes at 16kHz/16-bit/mono)
    /// Per S15-S16: PushAudioInputStream accepts raw PCM bytes
    /// </summary>
    public void PushPcm16k16bitMono(ReadOnlySpan<byte> pcmFrame)
    {
        if (_push == null)
        {
            _logger.LogWarning("Cannot push audio - transcriber not started");
            return;
        }

        // Write raw PCM bytes to the push stream
        // Audio frames are typically 20 ms = 640 bytes per S5
        _push.Write(pcmFrame.ToArray());

        _framesReceived++;
        _bytesReceived += pcmFrame.Length;

        // Log stats periodically (every 50 frames = ~1 second per S5)
        if (_framesReceived % 50 == 0)
        {
            _logger.LogDebug(
                "Audio stats: {Frames} frames, {Bytes} bytes ({Seconds:F1}s)",
                _framesReceived,
                _bytesReceived,
                _framesReceived * 0.02); // 20ms per frame
        }
    }

    /// <summary>
    /// Stop continuous recognition
    /// </summary>
    public async Task StopAsync()
    {
        if (_recognizer == null)
        {
            return;
        }

        _logger.LogInformation("Stopping speech recognition...");

        try
        {
            await _recognizer.StopContinuousRecognitionAsync().ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error stopping speech recognizer");
        }

        _recognizer.Dispose();
        _recognizer = null;

        _push?.Close();
        _push = null;

        _logger.LogInformation(
            "Speech recognition stopped. Total: {Frames} frames, {Bytes} bytes",
            _framesReceived,
            _bytesReceived);
    }

    /// <summary>
    /// Fire-and-forget publish to Python (don't block audio thread)
    /// </summary>
    private void FireAndForget(string kind, string? text, string? details = null)
    {
        var evt = new TranscriptEvent(kind, text, DateTime.UtcNow.ToString("O"), details);
        _ = Task.Run(() => _publisher.PublishAsync(evt));
    }

    public async ValueTask DisposeAsync()
    {
        await StopAsync().ConfigureAwait(false);
    }
}
