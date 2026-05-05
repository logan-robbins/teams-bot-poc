using Microsoft.Graph.Communications.Calls;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Graph.Communications.Resources;
using Microsoft.Graph.Models;
using Microsoft.Skype.Bots.Media;
using System.Buffers.Binary;
using System.Runtime.InteropServices;
using System.Threading;
using System.Timers;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Handles the lifecycle of a single call, including heartbeat, media, and transcription.
/// </summary>
/// <remarks>
/// <para>
/// Based on Microsoft's EchoBot CallHandler pattern.
/// </para>
/// <para>
/// Responsibilities:
/// <list type="bullet">
///   <item>Sends heartbeat keepalive every 10 minutes to prevent 45-minute timeout</item>
///   <item>Manages audio socket events and forwards frames to transcriber</item>
///   <item>Coordinates transcription lifecycle (start/stop)</item>
///   <item>Handles call state transitions</item>
/// </list>
/// </para>
/// </remarks>
public class CallHandler : HeartbeatHandler
{
    /// <summary>
    /// Heartbeat interval in minutes. Microsoft recommends between 15-45 minutes.
    /// Using 10 minutes for safety margin.
    /// </summary>
    private const int HeartbeatIntervalMinutes = 10;
    
    /// <summary>
    /// Number of frames between statistics logging (50 frames = ~1 second at 20ms/frame).
    /// </summary>
    private const int StatsLogInterval = 50;
    private const int AudioLevelStatsLogInterval = StatsLogInterval * 5;
    private const int UnmixedReadinessGraceSeconds = 10;
    private const int AudioReadinessGraceSeconds = 15;
    
    private readonly ILogger _logger;
    private readonly IRealtimeTranscriber _transcriber;
    private readonly bool _hasAudioSocket;
    private bool _isTranscriberStarted;
    private string _callState;
    private long _mediaFramesReceived;
    private long _audioFramesReceived;
    private long _unmixedAudioFramesReceived;
    private long _missingUnmixedFrames;
    private long _primaryAudioFramesReceived;
    private long _emptyAudioPayloadFrames;
    private long _audioLevelSampleCount;
    private long _audioLevelAbsSampleSum;
    private int _audioLevelPeak;
    private int _recentPeakSample;
    private double _recentAverageAbsSample;
    private long _establishedAtUtcTicks;
    private long _transcriptionStartedAtUtcTicks;
    private long _lastMediaFrameAtUtcTicks;
    private long _lastUnmixedAudioAtUtcTicks;
    private long _lastPrimaryMixedAudioAtUtcTicks;
    private long _lastNonSilentAudioAtUtcTicks;
    private bool _isShuttingDown;
    private bool _loggedFirstUnmixedAudio;
    private bool _loggedFirstPrimaryAudio;
    private bool _loggedMissingUnmixedAudio;
    private uint _lastDominantSpeaker = DominantSpeakerChangedEventArgs.None;
    /// <summary>
    /// E3: Snapshot of the most recent <c>AudioMediaBuffer.ActiveSpeakers</c>
    /// MediaSourceIds. Volatile-read pattern: written from the media worker
    /// thread inside <see cref="OnAudioMediaReceived"/>, read from the
    /// transcriber's publish thread via <see cref="GetMediaSourceIdHint"/>.
    /// </summary>
    private uint[]? _lastActiveSpeakers;

    /// <summary>
    /// Gets the call being handled.
    /// </summary>
    public ICall Call { get; }

    /// <summary>
    /// Gets the media session for this call.
    /// </summary>
    public ILocalMediaSession MediaSession { get; }

    /// <summary>
    /// Gets the UTC timestamp when this call was joined.
    /// </summary>
    public DateTime JoinedAtUtc { get; }

    /// <summary>
    /// Initializes a new instance of the <see cref="CallHandler"/> class.
    /// </summary>
    /// <param name="call">The stateful call object from Graph Communications SDK.</param>
    /// <param name="mediaSession">The local media session with audio socket.</param>
    /// <param name="transcriber">The real-time transcriber for this call.</param>
    /// <param name="logger">The application logger.</param>
    /// <exception cref="ArgumentNullException">Thrown when required parameters are null.</exception>
    public CallHandler(
        ICall call,
        ILocalMediaSession mediaSession,
        IRealtimeTranscriber transcriber,
        ILogger logger)
        : base(TimeSpan.FromMinutes(HeartbeatIntervalMinutes), call?.GraphLogger 
            ?? throw new ArgumentNullException(nameof(call)))
    {
        ArgumentNullException.ThrowIfNull(call);
        ArgumentNullException.ThrowIfNull(mediaSession);
        ArgumentNullException.ThrowIfNull(transcriber);
        ArgumentNullException.ThrowIfNull(logger);
        
        Call = call;
        MediaSession = mediaSession;
        _transcriber = transcriber;
        _logger = logger;
        JoinedAtUtc = DateTime.UtcNow;
        _callState = call.Resource.State?.ToString() ?? "Unknown";
        _hasAudioSocket = MediaSession.AudioSocket is not null;
        if (call.Resource.State == CallState.Established)
        {
            _establishedAtUtcTicks = JoinedAtUtc.Ticks;
        }

        // Subscribe to call state changes
        Call.OnUpdated += OnCallUpdated;

        // Subscribe to audio events
        if (_hasAudioSocket)
        {
            MediaSession.AudioSocket!.AudioMediaReceived += OnAudioMediaReceived;
            MediaSession.AudioSocket.DominantSpeakerChanged += OnDominantSpeakerChanged;
            _logger.LogInformation("CallHandler created for call {CallId} - audio socket wired", call.Id);
        }
        else
        {
            _logger.LogWarning("CallHandler created for call {CallId} - NO AUDIO SOCKET", call.Id);
        }
    }

    /// <summary>
    /// Sends keepalive to Microsoft Graph to prevent call from timing out.
    /// </summary>
    /// <remarks>
    /// Per Microsoft Graph API documentation: Calls without keepalive for 45 minutes are terminated.
    /// Source: https://learn.microsoft.com/en-us/graph/api/call-keepalive
    /// </remarks>
    protected override async Task HeartbeatAsync(ElapsedEventArgs args)
    {
        if (_isShuttingDown)
        {
            return;
        }
        
        try
        {
            _logger.LogDebug("Sending keepalive for call {CallId}", Call.Id);
            await Call.KeepAliveAsync().ConfigureAwait(false);
            _logger.LogDebug("Keepalive sent successfully for call {CallId}", Call.Id);
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            _logger.LogError(ex, "Failed to send keepalive for call {CallId}", Call.Id);
            // Don't throw - we want to keep trying on next heartbeat
        }
    }

    /// <summary>
    /// Handles call state changes from the Graph Communications SDK.
    /// </summary>
    private async void OnCallUpdated(ICall sender, ResourceEventArgs<Call> args)
    {
        var newState = args.NewResource.State;
        var oldState = args.OldResource.State;
        _callState = newState?.ToString() ?? "Unknown";

        _logger.LogInformation(
            "Call {CallId} state changed: {OldState} -> {NewState} (ResultInfo: {ResultInfo})",
            Call.Id,
            oldState,
            newState,
            args.NewResource.ResultInfo?.Message);

        // Start transcription when call is established
        if (oldState != newState && newState == CallState.Established)
        {
            Interlocked.Exchange(ref _establishedAtUtcTicks, DateTime.UtcNow.Ticks);
            _logger.LogInformation("Call {CallId} established - starting transcription", Call.Id);
            await StartTranscriptionAsync().ConfigureAwait(false);
        }

        // Stop transcription when call terminates
        if (oldState == CallState.Established && newState == CallState.Terminated)
        {
            _logger.LogInformation("Call {CallId} terminated - stopping transcription", Call.Id);
            await StopTranscriptionAsync().ConfigureAwait(false);
        }
    }

    private void OnDominantSpeakerChanged(object? sender, DominantSpeakerChangedEventArgs e)
    {
        _lastDominantSpeaker = e.CurrentDominantSpeaker;
        _logger.LogDebug(
            "Call {CallId}: dominant speaker changed to MediaSourceId={MediaSourceId}",
            Call.Id,
            e.CurrentDominantSpeaker);
    }

    /// <summary>
    /// E3: Returns the current Teams MediaSourceId hint (dominant + active set)
    /// for stamping onto the next published TranscriptEvent. Returns
    /// <c>(null, null)</c> when no dominant speaker has been observed yet.
    /// </summary>
    public (uint? Dominant, uint[]? Active) GetMediaSourceIdHint()
    {
        var dominant = _lastDominantSpeaker;
        return (
            dominant == DominantSpeakerChangedEventArgs.None ? null : dominant,
            Volatile.Read(ref _lastActiveSpeakers));
    }

    public CallMediaReadinessSnapshot GetMediaReadinessSnapshot(string threadId)
    {
        var now = DateTime.UtcNow;
        var establishedAtUtc = ReadUtc(_establishedAtUtcTicks);
        var transcriptionStartedAtUtc = ReadUtc(_transcriptionStartedAtUtcTicks);
        var lastMediaFrameAtUtc = ReadUtc(_lastMediaFrameAtUtcTicks);
        var lastUnmixedAudioAtUtc = ReadUtc(_lastUnmixedAudioAtUtcTicks);
        var lastPrimaryMixedAudioAtUtc = ReadUtc(_lastPrimaryMixedAudioAtUtcTicks);
        var lastNonSilentAudioAtUtc = ReadUtc(_lastNonSilentAudioAtUtcTicks);
        var (dominant, active) = GetMediaSourceIdHint();
        var readinessAge = establishedAtUtc.HasValue
            ? now - establishedAtUtc.Value
            : now - JoinedAtUtc;

        var (readiness, reason) = ResolveMediaReadiness(
            readinessAge,
            establishedAtUtc.HasValue,
            lastMediaFrameAtUtc,
            lastNonSilentAudioAtUtc);

        return new CallMediaReadinessSnapshot(
            ThreadId: threadId,
            CallId: Call.Id,
            State: _callState,
            JoinedAtUtc: JoinedAtUtc,
            EstablishedAtUtc: establishedAtUtc,
            TranscriptionStartedAtUtc: transcriptionStartedAtUtc,
            HasAudioSocket: _hasAudioSocket,
            TranscriberStarted: _isTranscriberStarted,
            RequiresUnmixedAudio: true,
            Readiness: readiness,
            ReadinessReason: reason,
            MediaFramesReceived: _mediaFramesReceived,
            TranscribedAudioFrames: _audioFramesReceived,
            UnmixedAudioFrames: _unmixedAudioFramesReceived,
            PrimaryMixedAudioFrames: _primaryAudioFramesReceived,
            FramesWithoutUnmixedBuffers: _missingUnmixedFrames,
            EmptyAudioPayloadFrames: _emptyAudioPayloadFrames,
            LastMediaFrameAtUtc: lastMediaFrameAtUtc,
            LastUnmixedAudioAtUtc: lastUnmixedAudioAtUtc,
            LastPrimaryMixedAudioAtUtc: lastPrimaryMixedAudioAtUtc,
            LastNonSilentAudioAtUtc: lastNonSilentAudioAtUtc,
            RecentPeakSample: _recentPeakSample,
            RecentAverageAbsSample: _recentAverageAbsSample,
            DominantMediaSourceId: dominant,
            ActiveMediaSourceIds: active);
    }

    private (string Readiness, string? Reason) ResolveMediaReadiness(
        TimeSpan readinessAge,
        bool isEstablished,
        DateTime? lastMediaFrameAtUtc,
        DateTime? lastNonSilentAudioAtUtc)
    {
        if (!_hasAudioSocket)
        {
            return ("no_audio_socket", "Call established without a local audio socket.");
        }

        if (!isEstablished)
        {
            return ("waiting_for_established", "Graph call has not reached Established state.");
        }

        if (!_isTranscriberStarted)
        {
            return ("waiting_for_transcriber", "Call is established but the STT transcriber is not started.");
        }

        if (lastMediaFrameAtUtc is null)
        {
            return readinessAge.TotalSeconds >= UnmixedReadinessGraceSeconds
                ? ("media_not_flowing", "Call is established but no media frames have arrived.")
                : ("waiting_for_media", "Call is established; waiting for first media frame.");
        }

        if (_unmixedAudioFramesReceived == 0)
        {
            return readinessAge.TotalSeconds >= UnmixedReadinessGraceSeconds
                ? ("unmixed_audio_missing", "Media frames are arriving, but Teams has not provided UnmixedAudioBuffers.")
                : ("waiting_for_unmixed_audio", "Media frames are arriving; waiting for Teams unmixed speaker buffers.");
        }

        if (lastNonSilentAudioAtUtc is null)
        {
            return readinessAge.TotalSeconds >= AudioReadinessGraceSeconds
                ? ("silent_audio", "Unmixed audio buffers are arriving, but all observed PCM samples are zero.")
                : ("waiting_for_non_silent_audio", "Unmixed audio buffers are arriving; waiting for non-silent PCM samples.");
        }

        return ("ready", null);
    }

    private static DateTime? ReadUtc(long ticks)
    {
        return ticks <= 0 ? null : new DateTime(ticks, DateTimeKind.Utc);
    }

    /// <summary>
    /// Handles incoming audio frames from Teams Media SDK.
    /// </summary>
    /// <remarks>
    /// Audio frames are 20ms each, delivered at ~50 fps (16kHz, 16-bit, mono PCM = 640 bytes per frame).
    /// This callback runs on a media worker thread and must not block.
    /// </remarks>
    private void OnAudioMediaReceived(object? sender, AudioMediaReceivedEventArgs e)
    {
        AudioMediaBuffer? buffer = null;
        
        try
        {
            buffer = e.Buffer;
            if (buffer is null)
            {
                return;
            }

            _mediaFramesReceived++;
            Interlocked.Exchange(ref _lastMediaFrameAtUtcTicks, DateTime.UtcNow.Ticks);
            var unmixedBuffers = buffer.UnmixedAudioBuffers;
            var pcmData = Array.Empty<byte>();
            uint[]? activeSpeakersForHint = null;
            var unmixedBufferCount = unmixedBuffers?.Length ?? 0;

            if (unmixedBufferCount > 0)
            {
                pcmData = MixUnmixedPcm16k16bitMono(unmixedBuffers!);
                activeSpeakersForHint = GetActiveSpeakerIds(buffer, unmixedBuffers!);
                _unmixedAudioFramesReceived++;
                Interlocked.Exchange(ref _lastUnmixedAudioAtUtcTicks, DateTime.UtcNow.Ticks);

                if (!_loggedFirstUnmixedAudio)
                {
                    _loggedFirstUnmixedAudio = true;
                    _logger.LogInformation(
                        "Call {CallId}: receiving unmixed Teams audio buffers for transcription. ActiveSpeakers={ActiveSpeakerCount}, UnmixedBuffers={UnmixedBufferCount}",
                        Call.Id,
                        activeSpeakersForHint?.Length ?? 0,
                        unmixedBufferCount);
                }
            }
            else
            {
                _missingUnmixedFrames++;
                pcmData = CopyPrimaryPcm16k16bitMono(buffer);
                activeSpeakersForHint = buffer.ActiveSpeakers;

                if (pcmData.Length > 0)
                {
                    _primaryAudioFramesReceived++;
                    Interlocked.Exchange(ref _lastPrimaryMixedAudioAtUtcTicks, DateTime.UtcNow.Ticks);

                    if (!_loggedFirstPrimaryAudio)
                    {
                        _loggedFirstPrimaryAudio = true;
                        _logger.LogInformation(
                            "Call {CallId}: receiving primary mixed Teams audio buffers for transcription because unmixed buffers are absent. FrameBytes={FrameBytes}, ActiveSpeakers={ActiveSpeakerCount}",
                            Call.Id,
                            pcmData.Length,
                            activeSpeakersForHint?.Length ?? 0);
                    }
                }
                else
                {
                    _emptyAudioPayloadFrames++;
                    if (!_loggedMissingUnmixedAudio)
                    {
                        _loggedMissingUnmixedAudio = true;
                        _logger.LogWarning(
                            "Call {CallId}: received media frame without unmixed audio buffers or primary PCM audio data.",
                            Call.Id);
                    }

                    if (_emptyAudioPayloadFrames % StatsLogInterval == 0)
                    {
                        _logger.LogInformation(
                            "Call {CallId}: received {MediaFrameCount} media frames, but {EmptyAudioPayloadFrameCount} had no Teams audio payload.",
                            Call.Id,
                            _mediaFramesReceived,
                            _emptyAudioPayloadFrames);
                    }
                }
            }

            if (pcmData.Length == 0)
            {
                return;
            }

            // Push to transcriber (non-blocking call)
            _transcriber.PushPcm16k16bitMono(pcmData);
            RecordAudioLevel(pcmData);

            _audioFramesReceived++;

            // E3: snapshot the most recent active-speakers set so the
            // transcriber can stamp it on each published event.
            if (activeSpeakersForHint is not null && activeSpeakersForHint.Length > 0)
            {
                Volatile.Write(ref _lastActiveSpeakers, activeSpeakersForHint);
            }

            // Log stats periodically (~1 second intervals)
            if (_audioFramesReceived % StatsLogInterval == 0)
            {
                _logger.LogDebug(
                    "Call {CallId}: Received {FrameCount} audio frames ({DurationSeconds:F1}s of audio), ActiveSpeakers={ActiveSpeakerCount}, UnmixedBuffers={UnmixedBufferCount}, DominantSpeaker={DominantSpeaker}",
                    Call.Id,
                    _audioFramesReceived,
                    _audioFramesReceived * 0.02,
                    activeSpeakersForHint?.Length ?? 0,
                    unmixedBufferCount,
                    _lastDominantSpeaker);
            }

            if (_audioFramesReceived % AudioLevelStatsLogInterval == 0)
            {
                var averageAbsSample = _audioLevelSampleCount == 0
                    ? 0
                    : (double)_audioLevelAbsSampleSum / _audioLevelSampleCount;
                _recentPeakSample = _audioLevelPeak;
                _recentAverageAbsSample = averageAbsSample;

                _logger.LogInformation(
                    "Call {CallId}: audio level stats Frames={FrameCount}, DurationSeconds={DurationSeconds:F1}, UnmixedFrames={UnmixedFrameCount}, PrimaryMixedFrames={PrimaryAudioFrameCount}, PeakSample={PeakSample}, AverageAbsSample={AverageAbsSample:F1}, ActiveSpeakers={ActiveSpeakerCount}, DominantSpeaker={DominantSpeaker}",
                    Call.Id,
                    _audioFramesReceived,
                    _audioFramesReceived * 0.02,
                    _unmixedAudioFramesReceived,
                    _primaryAudioFramesReceived,
                    _audioLevelPeak,
                    averageAbsSample,
                    activeSpeakersForHint?.Length ?? 0,
                    _lastDominantSpeaker);

                _audioLevelSampleCount = 0;
                _audioLevelAbsSampleSum = 0;
                _audioLevelPeak = 0;
            }
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error processing audio frame for call {CallId}", Call.Id);
        }
        finally
        {
            // CRITICAL: Must dispose buffer per SDK requirements to release unmanaged memory
            buffer?.Dispose();
        }
    }

    private static uint[]? GetActiveSpeakerIds(AudioMediaBuffer buffer, UnmixedAudioBuffer[] unmixedBuffers)
    {
        var activeSpeakers = buffer.ActiveSpeakers;
        if (activeSpeakers is not null && activeSpeakers.Length > 0)
        {
            return activeSpeakers;
        }

        return unmixedBuffers
            .Select(unmixed => unmixed.ActiveSpeakerId)
            .Where(activeSpeakerId => activeSpeakerId != 0)
            .Distinct()
            .ToArray();
    }

    private static byte[] MixUnmixedPcm16k16bitMono(UnmixedAudioBuffer[] unmixedBuffers)
    {
        var maxLength = 0;
        foreach (var unmixedBuffer in unmixedBuffers)
        {
            if (unmixedBuffer.Data != IntPtr.Zero && unmixedBuffer.Length > maxLength)
            {
                maxLength = checked((int)unmixedBuffer.Length);
            }
        }

        if (maxLength < 2)
        {
            return Array.Empty<byte>();
        }

        // 16-bit PCM samples must be whole little-endian Int16 values.
        maxLength -= maxLength % 2;
        var sampleCount = maxLength / 2;
        var mixedSamples = new int[sampleCount];

        foreach (var unmixedBuffer in unmixedBuffers)
        {
            if (unmixedBuffer.Data == IntPtr.Zero || unmixedBuffer.Length < 2)
            {
                continue;
            }

            var copyLength = Math.Min(checked((int)unmixedBuffer.Length), maxLength);
            copyLength -= copyLength % 2;
            var sourceBytes = new byte[copyLength];
            Marshal.Copy(unmixedBuffer.Data, sourceBytes, 0, copyLength);

            for (var byteIndex = 0; byteIndex < copyLength; byteIndex += 2)
            {
                mixedSamples[byteIndex / 2] += BinaryPrimitives.ReadInt16LittleEndian(
                    sourceBytes.AsSpan(byteIndex, 2));
            }
        }

        var mixedBytes = new byte[maxLength];
        for (var sampleIndex = 0; sampleIndex < sampleCount; sampleIndex++)
        {
            var clampedSample = Math.Clamp(
                mixedSamples[sampleIndex],
                short.MinValue,
                short.MaxValue);
            BinaryPrimitives.WriteInt16LittleEndian(
                mixedBytes.AsSpan(sampleIndex * 2, 2),
                (short)clampedSample);
        }

        return mixedBytes;
    }

    private static byte[] CopyPrimaryPcm16k16bitMono(AudioMediaBuffer buffer)
    {
        if (buffer.Data == IntPtr.Zero || buffer.Length < 2)
        {
            return Array.Empty<byte>();
        }

        var copyLength = checked((int)buffer.Length);
        copyLength -= copyLength % 2;
        if (copyLength == 0)
        {
            return Array.Empty<byte>();
        }

        var pcmData = new byte[copyLength];
        Marshal.Copy(buffer.Data, pcmData, 0, copyLength);
        return pcmData;
    }

    private void RecordAudioLevel(ReadOnlySpan<byte> pcmData)
    {
        for (var byteIndex = 0; byteIndex + 1 < pcmData.Length; byteIndex += 2)
        {
            var sample = BinaryPrimitives.ReadInt16LittleEndian(pcmData[byteIndex..(byteIndex + 2)]);
            var absSample = Math.Abs((int)sample);
            _audioLevelAbsSampleSum += absSample;
            _audioLevelSampleCount++;
            if (absSample > 0)
            {
                Interlocked.Exchange(ref _lastNonSilentAudioAtUtcTicks, DateTime.UtcNow.Ticks);
            }
            if (absSample > _audioLevelPeak)
            {
                _audioLevelPeak = absSample;
            }
        }
    }

    /// <summary>
    /// Starts the transcriber for this call.
    /// </summary>
    private async Task StartTranscriptionAsync()
    {
        if (_isTranscriberStarted || _isShuttingDown)
        {
            return;
        }

        try
        {
            await _transcriber.StartAsync().ConfigureAwait(false);
            _isTranscriberStarted = true;
            Interlocked.Exchange(ref _transcriptionStartedAtUtcTicks, DateTime.UtcNow.Ticks);
            _logger.LogInformation("Transcription started for call {CallId}", Call.Id);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to start transcription for call {CallId}", Call.Id);
        }
    }

    /// <summary>
    /// Stops the transcriber for this call.
    /// </summary>
    private async Task StopTranscriptionAsync()
    {
        if (!_isTranscriberStarted)
        {
            return;
        }

        try
        {
            await _transcriber.StopAsync().ConfigureAwait(false);
            _isTranscriberStarted = false;
            _logger.LogInformation(
                "Transcription stopped for call {CallId}. Total media frames: {MediaFrameCount}, transcribed audio frames: {AudioFrameCount}, unmixed audio frames: {UnmixedFrameCount}, primary mixed audio frames: {PrimaryAudioFrameCount}, frames without unmixed buffers: {MissingUnmixedFrameCount}, empty audio payload frames: {EmptyAudioPayloadFrameCount}",
                Call.Id,
                _mediaFramesReceived,
                _audioFramesReceived,
                _unmixedAudioFramesReceived,
                _primaryAudioFramesReceived,
                _missingUnmixedFrames,
                _emptyAudioPayloadFrames);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to stop transcription for call {CallId}", Call.Id);
        }
    }

    /// <summary>
    /// Gracefully shuts down this call handler, stopping transcription and unsubscribing from events.
    /// </summary>
    /// <returns>A task representing the asynchronous shutdown operation.</returns>
    public async Task ShutdownAsync()
    {
        if (_isShuttingDown)
        {
            return;
        }

        _isShuttingDown = true;
        _logger.LogInformation("Shutting down CallHandler for call {CallId}", Call.Id);

        // Stop transcription first
        await StopTranscriptionAsync().ConfigureAwait(false);

        // Dispose transcriber if it implements IAsyncDisposable
        if (_transcriber is IAsyncDisposable asyncDisposable)
        {
            try
            {
                await asyncDisposable.DisposeAsync().ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Error disposing transcriber for call {CallId}", Call.Id);
            }
        }

        // Unsubscribe from events to prevent memory leaks
        Call.OnUpdated -= OnCallUpdated;
        
        if (MediaSession.AudioSocket is not null)
        {
            MediaSession.AudioSocket.AudioMediaReceived -= OnAudioMediaReceived;
            MediaSession.AudioSocket.DominantSpeakerChanged -= OnDominantSpeakerChanged;
        }

        _logger.LogInformation(
            "CallHandler shutdown complete for call {CallId}. Duration: {Duration}",
            Call.Id,
            DateTime.UtcNow - JoinedAtUtc);
    }

    /// <inheritdoc/>
    protected override void Dispose(bool disposing)
    {
        if (disposing && !_isShuttingDown)
        {
            // Fire and forget shutdown - we're being disposed synchronously
            // but need to perform async cleanup
            _ = Task.Run(async () =>
            {
                try
                {
                    await ShutdownAsync().ConfigureAwait(false);
                }
                catch (Exception ex)
                {
                    _logger.LogError(ex, "Error during CallHandler dispose for call {CallId}", Call.Id);
                }
            });
        }

        base.Dispose(disposing);
    }
}
