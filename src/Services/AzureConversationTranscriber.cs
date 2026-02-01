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
