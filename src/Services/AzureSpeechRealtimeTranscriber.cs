using Microsoft.CognitiveServices.Speech;
using Microsoft.CognitiveServices.Speech.Audio;
using Microsoft.Extensions.Logging;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Factory for creating AzureSpeechRealtimeTranscriber instances
/// This avoids DI disposal issues since the transcriber implements IAsyncDisposable
/// and its lifetime is managed by CallHandler, not the DI container
/// </summary>
public class TranscriberFactory
{
    private readonly string _speechKey;
    private readonly string _speechRegion;
    private readonly string _language;
    private readonly string _pythonEndpoint;
    private readonly ILoggerFactory _loggerFactory;

    public TranscriberFactory(
        string speechKey,
        string speechRegion,
        string language,
        string pythonEndpoint,
        ILoggerFactory loggerFactory)
    {
        _speechKey = speechKey;
        _speechRegion = speechRegion;
        _language = language;
        _pythonEndpoint = pythonEndpoint;
        _loggerFactory = loggerFactory;
    }

    public AzureSpeechRealtimeTranscriber Create()
    {
        var publisherLogger = _loggerFactory.CreateLogger<PythonTranscriptPublisher>();
        var publisher = new PythonTranscriptPublisher(_pythonEndpoint, publisherLogger);
        
        var transcriberLogger = _loggerFactory.CreateLogger<AzureSpeechRealtimeTranscriber>();
        return new AzureSpeechRealtimeTranscriber(
            _speechKey,
            _speechRegion,
            _language,
            publisher,
            transcriberLogger);
    }
}

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
    
    // Transcript file path - save to Desktop for easy viewing
    private static readonly string TranscriptFilePath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.Desktop),
        "meeting_transcript.txt");
    private static readonly object _fileLock = new();

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
    /// Also saves to desktop file for easy viewing
    /// </summary>
    private void FireAndForget(string kind, string? text, string? details = null)
    {
        var timestamp = DateTime.UtcNow.ToString("O");
        var evt = new TranscriptEvent(kind, text, timestamp, details);
        
        // Publish to Python endpoint
        _ = Task.Run(() => _publisher.PublishAsync(evt));
        
        // Also save to desktop file
        _ = Task.Run(() => SaveToFile(kind, text, timestamp));
    }
    
    /// <summary>
    /// Save transcript to file on desktop
    /// </summary>
    private void SaveToFile(string kind, string? text, string timestamp)
    {
        try
        {
            lock (_fileLock)
            {
                using var writer = new StreamWriter(TranscriptFilePath, append: true);
                
                switch (kind)
                {
                    case "session_started":
                        writer.WriteLine();
                        writer.WriteLine(new string('=', 60));
                        writer.WriteLine($"NEW SESSION STARTED: {timestamp}");
                        writer.WriteLine(new string('=', 60));
                        writer.WriteLine();
                        _logger.LogInformation("Transcript file: {Path}", TranscriptFilePath);
                        break;
                        
                    case "recognized" when !string.IsNullOrWhiteSpace(text):
                        // Only save final transcripts (not partial)
                        writer.WriteLine($"[{timestamp}] {text}");
                        break;
                        
                    case "session_stopped":
                        writer.WriteLine();
                        writer.WriteLine($"--- Session ended: {timestamp} ---");
                        writer.WriteLine();
                        break;
                }
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to save transcript to file");
        }
    }

    public async ValueTask DisposeAsync()
    {
        await StopAsync().ConfigureAwait(false);
    }
}
