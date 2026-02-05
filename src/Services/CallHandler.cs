using Microsoft.Graph.Communications.Calls;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Graph.Communications.Resources;
using Microsoft.Graph.Models;
using Microsoft.Skype.Bots.Media;
using System.Runtime.InteropServices;
using System.Timers;

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
    
    private readonly ILogger _logger;
    private readonly IRealtimeTranscriber _transcriber;
    private bool _isTranscriberStarted;
    private long _audioFramesReceived;
    private bool _isShuttingDown;

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

        // Subscribe to call state changes
        Call.OnUpdated += OnCallUpdated;

        // Subscribe to audio events
        if (MediaSession.AudioSocket is not null)
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

        _logger.LogInformation(
            "Call {CallId} state changed: {OldState} -> {NewState} (ResultInfo: {ResultInfo})",
            Call.Id,
            oldState,
            newState,
            args.NewResource.ResultInfo?.Message);

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
            if (buffer is null || buffer.Data == IntPtr.Zero || buffer.Length <= 0)
            {
                return;
            }

            // Extract PCM bytes from unmanaged buffer
            // Note: Using stackalloc would be better for small buffers, but buffer.Length 
            // can vary and we need to pass to transcriber which expects heap allocation
            var pcmData = new byte[buffer.Length];
            Marshal.Copy(buffer.Data, pcmData, 0, (int)buffer.Length);

            // Push to transcriber (non-blocking call)
            _transcriber.PushPcm16k16bitMono(pcmData);

            _audioFramesReceived++;

            // Log stats periodically (~1 second intervals)
            if (_audioFramesReceived % StatsLogInterval == 0)
            {
                _logger.LogDebug(
                    "Call {CallId}: Received {FrameCount} audio frames ({DurationSeconds:F1}s of audio)",
                    Call.Id,
                    _audioFramesReceived,
                    _audioFramesReceived * 0.02);
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
                "Transcription stopped for call {CallId}. Total audio frames: {FrameCount}",
                Call.Id,
                _audioFramesReceived);
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
