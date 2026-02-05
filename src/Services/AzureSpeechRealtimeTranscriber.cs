using Microsoft.CognitiveServices.Speech;
using Microsoft.CognitiveServices.Speech.Audio;
using Microsoft.Extensions.Logging;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Factory for creating <see cref="IRealtimeTranscriber"/> instances.
/// </summary>
/// <remarks>
/// <para>
/// Supports Deepgram (primary) and Azure ConversationTranscriber (fallback).
/// </para>
/// <para>
/// This factory pattern avoids DI disposal issues since transcribers implement 
/// <see cref="IAsyncDisposable"/> and their lifetime is managed by CallHandler, 
/// not the DI container.
/// </para>
/// </remarks>
public sealed class TranscriberFactory
{
    private readonly SttConfiguration _sttConfig;
    private readonly string _pythonEndpoint;
    private readonly ILoggerFactory _loggerFactory;

    /// <summary>
    /// Initializes a new instance of the <see cref="TranscriberFactory"/> class.
    /// </summary>
    /// <param name="sttConfig">The STT configuration.</param>
    /// <param name="pythonEndpoint">The Python endpoint for transcript events.</param>
    /// <param name="loggerFactory">The logger factory.</param>
    /// <exception cref="ArgumentNullException">Thrown when required parameters are null.</exception>
    public TranscriberFactory(
        SttConfiguration sttConfig,
        string pythonEndpoint,
        ILoggerFactory loggerFactory)
    {
        ArgumentNullException.ThrowIfNull(sttConfig);
        ArgumentException.ThrowIfNullOrWhiteSpace(pythonEndpoint);
        ArgumentNullException.ThrowIfNull(loggerFactory);
        
        _sttConfig = sttConfig;
        _pythonEndpoint = pythonEndpoint;
        _loggerFactory = loggerFactory;
    }

    /// <summary>
    /// Creates a new transcriber instance based on the configured provider.
    /// </summary>
    /// <returns>A new <see cref="IRealtimeTranscriber"/> instance.</returns>
    /// <exception cref="InvalidOperationException">
    /// Thrown when the configured provider's configuration is missing.
    /// </exception>
    /// <exception cref="NotSupportedException">
    /// Thrown when the configured provider is not supported.
    /// </exception>
    public IRealtimeTranscriber Create()
    {
        var publisherLogger = _loggerFactory.CreateLogger<PythonTranscriptPublisher>();
        var publisher = new PythonTranscriptPublisher(_pythonEndpoint, publisherLogger);
        
        var provider = (_sttConfig.Provider ?? "Deepgram").Trim();

        // PRIMARY: Deepgram (best diarization quality)
        if (string.Equals(provider, "Deepgram", StringComparison.OrdinalIgnoreCase))
        {
            return CreateDeepgramTranscriber(publisher);
        }

        // FALLBACK: Azure Speech ConversationTranscriber
        if (string.Equals(provider, "AzureSpeech", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(provider, "Azure", StringComparison.OrdinalIgnoreCase))
        {
            return CreateAzureTranscriber(publisher);
        }

        throw new NotSupportedException(
            $"STT provider '{provider}' is not supported. Use 'Deepgram' or 'AzureSpeech'.");
    }

    /// <summary>
    /// Creates a Deepgram transcriber instance.
    /// </summary>
    private DeepgramRealtimeTranscriber CreateDeepgramTranscriber(PythonTranscriptPublisher publisher)
    {
        var config = _sttConfig.Deepgram ?? throw new InvalidOperationException(
            "STT provider 'Deepgram' selected but Stt.Deepgram config is missing.");

        var logger = _loggerFactory.CreateLogger<DeepgramRealtimeTranscriber>();
        return new DeepgramRealtimeTranscriber(
            config.ApiKey,
            config.Model,
            config.Diarize,
            publisher,
            logger);
    }

    /// <summary>
    /// Creates an Azure ConversationTranscriber instance.
    /// </summary>
    private AzureConversationTranscriber CreateAzureTranscriber(PythonTranscriptPublisher publisher)
    {
        var config = _sttConfig.AzureSpeech ?? throw new InvalidOperationException(
            "STT provider 'AzureSpeech' selected but Stt.AzureSpeech config is missing.");

        var logger = _loggerFactory.CreateLogger<AzureConversationTranscriber>();
        return new AzureConversationTranscriber(
            config.Key,
            config.Region,
            config.RecognitionLanguage,
            config.EndpointId,
            publisher,
            logger);
    }
}

/// <summary>
/// Real-time speech transcription using Azure Speech SDK (DEPRECATED - no diarization support).
/// 
/// NOTE: This class uses SpeechRecognizer which does NOT support diarization.
/// Use AzureConversationTranscriber instead for speaker identification.
/// 
/// Implements streaming PCM audio → continuous recognition → transcript events
/// Based on Part I (I3) and Part B (B2) of the validated guide
/// Sources: S15 (audio streams), S16 (PCM format), S17-S18 (continuous recognition)
/// </summary>
public sealed class AzureSpeechRealtimeTranscriber : IRealtimeTranscriber
{
    private readonly string _speechKey;
    private readonly string _speechRegion;
    private readonly string _lang;
    private readonly string? _endpointId;
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
        string? endpointId,
        PythonTranscriptPublisher publisher,
        ILogger<AzureSpeechRealtimeTranscriber> logger)
    {
        _speechKey = speechKey;
        _speechRegion = speechRegion;
        _lang = language;
        _endpointId = string.IsNullOrWhiteSpace(endpointId) ? null : endpointId.Trim();
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
        if (_endpointId != null)
        {
            // Optional: Custom Speech model selection.
            // When provided, the recognizer targets that specific model endpoint.
            cfg.EndpointId = _endpointId;
        }

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
    /// 
    /// NOTE: This class is deprecated. Use AzureConversationTranscriber for diarization support.
    /// </summary>
    private void FireAndForget(string kind, string? text, string? details = null)
    {
        var timestamp = DateTime.UtcNow.ToString("O");
        
        // Map old event types to new format
        var eventType = kind switch
        {
            "recognizing" => "partial",
            "recognized" => "final",
            "session_started" => "session_started",
            "session_stopped" => "session_stopped",
            "canceled" => "error",
            _ => kind
        };
        
        EventError? error = null;
        if (kind == "canceled" && !string.IsNullOrWhiteSpace(details))
        {
            error = new EventError("AZURE_SPEECH_ERROR", details);
        }
        
        var evt = new TranscriptEvent(
            EventType: eventType,
            Text: text,
            TimestampUtc: timestamp,
            Metadata: new EventMetadata(Provider: "azure_speech", Model: null, SessionId: null),
            Error: error
        );
        
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
