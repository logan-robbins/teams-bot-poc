using Microsoft.CognitiveServices.Speech;
using Microsoft.CognitiveServices.Speech.Audio;
using Microsoft.CognitiveServices.Speech.Transcription;
using Microsoft.Extensions.Logging;
using TeamsMediaBot.Models;
using System.Text.RegularExpressions;

namespace TeamsMediaBot.Services;

/// <summary>
/// Real-time diarized transcription using Azure Speech ConversationTranscriber.
/// </summary>
/// <remarks>
/// <para>
/// CRITICAL: This uses ConversationTranscriber, NOT SpeechRecognizer.
/// SpeechRecognizer does NOT support real-time diarization.
/// </para>
/// <para>
/// Speaker IDs: Azure returns "Guest-1", "Guest-2", etc.
/// We normalize to: "speaker_0", "speaker_1", etc. for consistency with Deepgram.
/// </para>
/// <para>
/// Documentation: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/get-started-stt-diarization
/// GA Announcement: https://techcommunity.microsoft.com/blog/azure-ai-services-blog/announcing-general-availability-of-real-time-diarization/4147556
/// </para>
/// </remarks>
public sealed partial class AzureConversationTranscriber : IRealtimeTranscriber
{
    private readonly string _speechKey;
    private readonly string _speechRegion;
    private readonly string _language;
    private readonly string? _endpointId;
    private readonly PythonTranscriptPublisher _publisher;
    private readonly ILogger<AzureConversationTranscriber> _logger;

    private PushAudioInputStream? _audioInputStream;
    private ConversationTranscriber? _transcriber;
    private string? _sessionId;
    private long _framesReceived;
    private long _bytesReceived;
    private bool _isDisposed;

    /// <summary>
    /// Source-generated regex to extract speaker number from "Guest-1", "Guest-2", etc.
    /// Using source generation for better startup performance and AOT compatibility.
    /// </summary>
    [GeneratedRegex(@"Guest-(\d+)", RegexOptions.Compiled | RegexOptions.CultureInvariant)]
    private static partial Regex SpeakerIdRegex();

    /// <summary>
    /// Initializes a new instance of the <see cref="AzureConversationTranscriber"/> class.
    /// </summary>
    /// <param name="speechKey">The Azure Speech subscription key.</param>
    /// <param name="speechRegion">The Azure Speech region (e.g., "eastus").</param>
    /// <param name="language">The recognition language (e.g., "en-US").</param>
    /// <param name="endpointId">Optional custom endpoint ID for Custom Speech models.</param>
    /// <param name="publisher">The publisher for sending transcript events to Python.</param>
    /// <param name="logger">The logger instance.</param>
    /// <exception cref="ArgumentNullException">Thrown when required parameters are null.</exception>
    public AzureConversationTranscriber(
        string speechKey,
        string speechRegion,
        string language,
        string? endpointId,
        PythonTranscriptPublisher publisher,
        ILogger<AzureConversationTranscriber> logger)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(speechKey);
        ArgumentException.ThrowIfNullOrWhiteSpace(speechRegion);
        ArgumentException.ThrowIfNullOrWhiteSpace(language);
        ArgumentNullException.ThrowIfNull(publisher);
        ArgumentNullException.ThrowIfNull(logger);
        
        _speechKey = speechKey;
        _speechRegion = speechRegion;
        _language = language;
        _endpointId = string.IsNullOrWhiteSpace(endpointId) ? null : endpointId.Trim();
        _publisher = publisher;
        _logger = logger;
    }

    /// <inheritdoc/>
    public async Task StartAsync(CancellationToken ct = default)
    {
        ObjectDisposedException.ThrowIf(_isDisposed, this);
        
        if (_transcriber != null)
        {
            _logger.LogWarning("Azure ConversationTranscriber already started");
            return;
        }

        _logger.LogInformation(
            "Starting Azure ConversationTranscriber (Region={Region}, Language={Language})",
            _speechRegion, _language);

        // Configure audio input - Teams sends 16kHz/16-bit/mono PCM
        _audioInputStream = AudioInputStream.CreatePushStream(
            AudioStreamFormat.GetWaveFormatPCM(
                samplesPerSecond: 16000,
                bitsPerSample: 16,
                channels: 1));
        
        var audioConfig = AudioConfig.FromStreamInput(_audioInputStream);

        // Configure speech service
        var speechConfig = SpeechConfig.FromSubscription(_speechKey, _speechRegion);
        speechConfig.SpeechRecognitionLanguage = _language;
        
        // CRITICAL: Enable intermediate diarization results for real-time speaker attribution
        // Source: Azure docs - PropertyId.SpeechServiceResponse_DiarizeIntermediateResults
        speechConfig.SetProperty(
            PropertyId.SpeechServiceResponse_DiarizeIntermediateResults, 
            "true");

        // Optional: Custom Speech model endpoint
        if (_endpointId is not null)
        {
            speechConfig.EndpointId = _endpointId;
        }

        // Use ConversationTranscriber (NOT SpeechRecognizer) for diarization
        _transcriber = new ConversationTranscriber(speechConfig, audioConfig);
        _sessionId = Guid.NewGuid().ToString("N");

        // Wire up event handlers (using method groups for cleaner code)
        _transcriber.SessionStarted += OnSessionStarted;
        _transcriber.SessionStopped += OnSessionStopped;
        _transcriber.Transcribing += OnTranscribing;
        _transcriber.Transcribed += OnTranscribed;
        _transcriber.Canceled += OnCanceled;

        // Start continuous transcription
        await _transcriber.StartTranscribingAsync().ConfigureAwait(false);
        _logger.LogInformation("Azure ConversationTranscriber started successfully");
    }

    /// <summary>
    /// Handles the session started event.
    /// </summary>
    private void OnSessionStarted(object? sender, SessionEventArgs e)
    {
        _sessionId = e.SessionId;
        _logger.LogInformation("Azure session started: {SessionId}", _sessionId);
        PublishEventAsync("session_started", text: null, speakerId: null);
    }

    /// <summary>
    /// Handles the session stopped event.
    /// </summary>
    private void OnSessionStopped(object? sender, SessionEventArgs e)
    {
        _logger.LogInformation("Azure session stopped");
        PublishEventAsync("session_stopped", text: null, speakerId: null);
    }

    /// <summary>
    /// Handles transcribing (partial/interim) results with speaker identification.
    /// </summary>
    private void OnTranscribing(object? sender, ConversationTranscriptionEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(e.Result?.Text))
        {
            return;
        }
        
        var speakerId = NormalizeSpeakerId(e.Result.SpeakerId);
        _logger.LogDebug("[PARTIAL] Speaker={SpeakerId}: {Text}", speakerId ?? "unknown", e.Result.Text);
        PublishEventAsync("partial", e.Result.Text, speakerId);
    }

    /// <summary>
    /// Handles transcribed (final) results with speaker identification.
    /// </summary>
    private void OnTranscribed(object? sender, ConversationTranscriptionEventArgs e)
    {
        if (e.Result?.Reason != ResultReason.RecognizedSpeech || string.IsNullOrWhiteSpace(e.Result.Text))
        {
            return;
        }
        
        var speakerId = NormalizeSpeakerId(e.Result.SpeakerId);
        _logger.LogInformation("[FINAL] Speaker={SpeakerId}: {Text}", speakerId ?? "unknown", e.Result.Text);
        PublishEventAsync("final", e.Result.Text, speakerId);
    }

    /// <summary>
    /// Handles cancellation/error events.
    /// </summary>
    private void OnCanceled(object? sender, ConversationTranscriptionCanceledEventArgs e)
    {
        _logger.LogError(
            "Azure transcription canceled: Reason={Reason}, ErrorCode={ErrorCode}, Details={ErrorDetails}",
            e.Reason, e.ErrorCode, e.ErrorDetails);
        
        PublishEventAsync(
            "error",
            text: null,
            speakerId: null,
            error: new EventError(e.ErrorCode.ToString(), e.ErrorDetails ?? "Canceled"));
    }

    /// <summary>
    /// Normalizes Azure speaker ID from "Guest-1" to "speaker_0".
    /// </summary>
    /// <remarks>
    /// Azure uses 1-based indexing (Guest-1, Guest-2), we use 0-based 
    /// (speaker_0, speaker_1) for consistency with Deepgram.
    /// </remarks>
    private static string? NormalizeSpeakerId(string? azureSpeakerId)
    {
        if (string.IsNullOrWhiteSpace(azureSpeakerId) || 
            string.Equals(azureSpeakerId, "Unknown", StringComparison.OrdinalIgnoreCase))
        {
            return null;
        }

        var match = SpeakerIdRegex().Match(azureSpeakerId);
        if (match.Success && int.TryParse(match.Groups[1].Value, out var speakerNumber))
        {
            // Convert 1-based to 0-based indexing
            return $"speaker_{speakerNumber - 1}";
        }

        // Fallback: normalize the raw ID
        return azureSpeakerId.ToLowerInvariant().Replace("-", "_", StringComparison.Ordinal);
    }

    /// <inheritdoc/>
    /// <remarks>
    /// Teams Media SDK delivers 20ms frames at 16kHz/16-bit/mono = 640 bytes per frame.
    /// This method is called from the audio receive callback and must not block.
    /// </remarks>
    public void PushPcm16k16bitMono(ReadOnlySpan<byte> pcmFrame)
    {
        if (_isDisposed || _audioInputStream is null)
        {
            _logger.LogWarning("Cannot push audio - Azure transcriber not started or disposed");
            return;
        }

        // Note: ToArray() is required as the SDK doesn't accept Span<T>
        _audioInputStream.Write(pcmFrame.ToArray());

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
        if (_transcriber is null)
        {
            return;
        }

        _logger.LogInformation("Stopping Azure ConversationTranscriber...");

        // Unsubscribe from events to prevent memory leaks
        _transcriber.SessionStarted -= OnSessionStarted;
        _transcriber.SessionStopped -= OnSessionStopped;
        _transcriber.Transcribing -= OnTranscribing;
        _transcriber.Transcribed -= OnTranscribed;
        _transcriber.Canceled -= OnCanceled;

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

        _audioInputStream?.Close();
        _audioInputStream = null;

        _logger.LogInformation(
            "Azure stopped. Total: Frames={FrameCount}, Bytes={ByteCount}",
            _framesReceived,
            _bytesReceived);
    }

    /// <summary>
    /// Publishes a transcript event asynchronously without blocking.
    /// </summary>
    private void PublishEventAsync(string eventType, string? text, string? speakerId, EventError? error = null)
    {
        var transcriptEvent = new TranscriptEvent(
            EventType: eventType,
            Text: text,
            TimestampUtc: DateTime.UtcNow.ToString("O"),
            SpeakerId: speakerId,
            Metadata: new EventMetadata(Provider: "azure_speech", Model: null, SessionId: _sessionId),
            Error: error
        );

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
