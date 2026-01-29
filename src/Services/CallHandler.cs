using Microsoft.Graph.Communications.Calls;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Graph.Communications.Common.Telemetry;
using Microsoft.Graph.Communications.Resources;
using Microsoft.Graph.Models;
using System.Runtime.InteropServices;
using System.Timers;

namespace TeamsMediaBot.Services;

/// <summary>
/// Handles the lifecycle of a single call, including heartbeat, media, and transcription
/// Based on Microsoft's EchoBot CallHandler pattern
/// 
/// Responsibilities:
/// - Sends heartbeat keepalive every 10 minutes to prevent 45-minute timeout
/// - Manages audio socket events
/// - Coordinates transcription lifecycle
/// - Handles call state transitions
/// </summary>
public class CallHandler : HeartbeatHandler
{
    private readonly ILogger _logger;
    private readonly AzureSpeechRealtimeTranscriber _transcriber;
    private bool _isTranscriberStarted;
    private long _audioFramesReceived;

    /// <summary>
    /// Gets the call being handled
    /// </summary>
    public ICall Call { get; }

    /// <summary>
    /// Gets the media session for this call
    /// </summary>
    public ILocalMediaSession MediaSession { get; }

    /// <summary>
    /// Gets the timestamp when this call was joined
    /// </summary>
    public DateTime JoinedAt { get; }

    /// <summary>
    /// Initializes a new instance of the <see cref="CallHandler"/> class.
    /// </summary>
    /// <param name="call">The stateful call object</param>
    /// <param name="mediaSession">The media session</param>
    /// <param name="transcriber">The transcriber for this call</param>
    /// <param name="logger">The application logger</param>
    public CallHandler(
        ICall call,
        ILocalMediaSession mediaSession,
        AzureSpeechRealtimeTranscriber transcriber,
        ILogger logger)
        : base(TimeSpan.FromMinutes(10), call.GraphLogger) // Heartbeat every 10 minutes (Microsoft uses 10 min)
    {
        Call = call ?? throw new ArgumentNullException(nameof(call));
        MediaSession = mediaSession ?? throw new ArgumentNullException(nameof(mediaSession));
        _transcriber = transcriber ?? throw new ArgumentNullException(nameof(transcriber));
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
        JoinedAt = DateTime.UtcNow;

        // Subscribe to call state changes
        Call.OnUpdated += OnCallUpdated;

        // Subscribe to audio events
        if (MediaSession.AudioSocket != null)
        {
            MediaSession.AudioSocket.AudioMediaReceived += OnAudioMediaReceived;
            _logger.LogInformation("CallHandler created for call {CallId} - audio socket wired", call.Id);
        }
        else
        {
            _logger.LogWarning("CallHandler created for call {CallId} - NO AUDIO SOCKET", call.Id);
        }
    }

    /// <summary>
    /// Sends keepalive to Microsoft Graph to prevent call from timing out
    /// Per Microsoft: Calls without keepalive for 45 minutes are terminated
    /// </summary>
    protected override async Task HeartbeatAsync(ElapsedEventArgs args)
    {
        try
        {
            _logger.LogDebug("Sending keepalive for call {CallId}", Call.Id);
            await Call.KeepAliveAsync().ConfigureAwait(false);
            _logger.LogDebug("Keepalive sent successfully for call {CallId}", Call.Id);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to send keepalive for call {CallId}", Call.Id);
            // Don't throw - we want to keep trying on next heartbeat
        }
    }

    /// <summary>
    /// Handles call state changes
    /// </summary>
    private async void OnCallUpdated(ICall sender, ResourceEventArgs<Call> args)
    {
        var newState = args.NewResource.State;
        var oldState = args.OldResource.State;

        _logger.LogInformation(
            "Call {CallId} state changed: {OldState} -> {NewState} (ResultInfo: {ResultInfo})",
            Call.Id, oldState, newState, args.NewResource.ResultInfo?.Message);

        // Start transcription when call is established
        if (oldState != newState && newState == CallState.Established)
        {
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

    /// <summary>
    /// Handles incoming audio frames from Teams
    /// Per S5: Audio frames are 20ms each, delivered at ~50 fps (16kHz, 16-bit, mono PCM)
    /// </summary>
    private void OnAudioMediaReceived(object? sender, AudioMediaReceivedEventArgs e)
    {
        try
        {
            var buffer = e.Buffer;
            if (buffer != null && buffer.Data != IntPtr.Zero && buffer.Length > 0)
            {
                // Extract PCM bytes from unmanaged buffer
                var pcmData = new byte[buffer.Length];
                Marshal.Copy(buffer.Data, pcmData, 0, (int)buffer.Length);

                // Push to transcriber
                _transcriber.PushPcm16k16bitMono(pcmData);

                _audioFramesReceived++;

                // Log stats every second (~50 frames)
                if (_audioFramesReceived % 50 == 0)
                {
                    _logger.LogDebug(
                        "Call {CallId}: Received {Frames} audio frames ({Seconds:F1}s of audio)",
                        Call.Id, _audioFramesReceived, _audioFramesReceived * 0.02);
                }
            }
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error processing audio frame for call {CallId}", Call.Id);
        }
        finally
        {
            // CRITICAL: Must dispose buffer per SDK requirements
            e.Buffer?.Dispose();
        }
    }

    /// <summary>
    /// Starts the transcriber
    /// </summary>
    private async Task StartTranscriptionAsync()
    {
        if (_isTranscriberStarted) return;

        try
        {
            await _transcriber.StartAsync().ConfigureAwait(false);
            _isTranscriberStarted = true;
            _logger.LogInformation("Transcription started for call {CallId}", Call.Id);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to start transcription for call {CallId}", Call.Id);
        }
    }

    /// <summary>
    /// Stops the transcriber
    /// </summary>
    private async Task StopTranscriptionAsync()
    {
        if (!_isTranscriberStarted) return;

        try
        {
            await _transcriber.StopAsync().ConfigureAwait(false);
            _isTranscriberStarted = false;
            _logger.LogInformation(
                "Transcription stopped for call {CallId}. Total audio frames: {Frames}",
                Call.Id, _audioFramesReceived);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to stop transcription for call {CallId}", Call.Id);
        }
    }

    /// <summary>
    /// Gracefully shuts down this call handler
    /// </summary>
    public async Task ShutdownAsync()
    {
        _logger.LogInformation("Shutting down CallHandler for call {CallId}", Call.Id);

        // Stop transcription
        await StopTranscriptionAsync().ConfigureAwait(false);

        // Unsubscribe from events
        Call.OnUpdated -= OnCallUpdated;
        
        if (MediaSession.AudioSocket != null)
        {
            MediaSession.AudioSocket.AudioMediaReceived -= OnAudioMediaReceived;
        }
    }

    /// <inheritdoc/>
    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            // Fire and forget shutdown - we're being disposed
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
