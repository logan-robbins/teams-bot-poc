using Microsoft.Graph.Communications.Common;
using Microsoft.Graph.Communications.Common.Telemetry;
using System.Timers;
using Timer = System.Timers.Timer;

namespace TeamsMediaBot.Services;

/// <summary>
/// Base class for handling heartbeats to keep calls alive
/// Based on Microsoft's EchoBot HeartbeatHandler pattern
/// 
/// Per Microsoft Graph API documentation:
/// "Make a request to this API every 15 to 45 minutes to ensure that an ongoing call remains active.
/// A call that does not receive this request within 45 minutes is considered inactive and will
/// subsequently end."
/// 
/// Source: https://learn.microsoft.com/en-us/graph/api/call-keepalive
/// </summary>
public abstract class HeartbeatHandler : ObjectRootDisposable
{
    private readonly Timer _heartbeatTimer;

    /// <summary>
    /// Initializes a new instance of the <see cref="HeartbeatHandler"/> class.
    /// </summary>
    /// <param name="frequency">The frequency of the heartbeat (recommended: 10 minutes)</param>
    /// <param name="logger">The graph logger</param>
    protected HeartbeatHandler(TimeSpan frequency, IGraphLogger logger)
        : base(logger)
    {
        // Initialize the timer
        _heartbeatTimer = new Timer(frequency.TotalMilliseconds)
        {
            Enabled = true,
            AutoReset = true
        };
        _heartbeatTimer.Elapsed += HeartbeatDetected;
    }

    /// <summary>
    /// This function is called whenever the heartbeat frequency has elapsed.
    /// Implementations should call Call.KeepAliveAsync()
    /// </summary>
    /// <param name="args">The elapsed event args</param>
    /// <returns>A task representing the async operation</returns>
    protected abstract Task HeartbeatAsync(ElapsedEventArgs args);

    /// <inheritdoc/>
    protected override void Dispose(bool disposing)
    {
        base.Dispose(disposing);
        _heartbeatTimer.Elapsed -= HeartbeatDetected;
        _heartbeatTimer.Stop();
        _heartbeatTimer.Dispose();
    }

    /// <summary>
    /// Internal heartbeat handler that invokes the async heartbeat
    /// </summary>
    private void HeartbeatDetected(object? sender, ElapsedEventArgs args)
    {
        var task = $"{GetType().FullName}.{nameof(HeartbeatAsync)}(args)";
        GraphLogger.Verbose($"Starting heartbeat task: {task}");
        
        // Fire and forget, but log any exceptions
        _ = Task.Run(async () =>
        {
            try
            {
                await HeartbeatAsync(args).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                GraphLogger.Error(ex, $"Heartbeat failed: {task}");
            }
        });
    }
}
