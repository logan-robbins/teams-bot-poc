using Microsoft.Graph.Communications.Common;
using Microsoft.Graph.Communications.Common.Telemetry;
using System.Timers;
using Timer = System.Timers.Timer;

namespace TeamsMediaBot.Services;

/// <summary>
/// Base class for handling heartbeats to keep calls alive.
/// </summary>
/// <remarks>
/// <para>
/// Based on Microsoft's EchoBot HeartbeatHandler pattern.
/// </para>
/// <para>
/// Per Microsoft Graph API documentation:
/// "Make a request to this API every 15 to 45 minutes to ensure that an ongoing call remains active.
/// A call that does not receive this request within 45 minutes is considered inactive and will
/// subsequently end."
/// </para>
/// <para>
/// Source: https://learn.microsoft.com/en-us/graph/api/call-keepalive
/// </para>
/// </remarks>
public abstract class HeartbeatHandler : ObjectRootDisposable
{
    private readonly Timer _heartbeatTimer;

    /// <summary>
    /// Initializes a new instance of the <see cref="HeartbeatHandler"/> class.
    /// </summary>
    /// <param name="frequency">The frequency of the heartbeat (recommended: 10 minutes).</param>
    /// <param name="logger">The Graph Communications logger.</param>
    /// <exception cref="ArgumentNullException">Thrown when logger is null.</exception>
    /// <exception cref="ArgumentOutOfRangeException">Thrown when frequency is not positive.</exception>
    protected HeartbeatHandler(TimeSpan frequency, IGraphLogger logger)
        : base(logger ?? throw new ArgumentNullException(nameof(logger)))
    {
        if (frequency <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(frequency), "Frequency must be positive.");
        }

        // Initialize the timer
        _heartbeatTimer = new Timer(frequency.TotalMilliseconds)
        {
            Enabled = true,
            AutoReset = true
        };
        _heartbeatTimer.Elapsed += OnHeartbeatTimerElapsed;
    }

    /// <summary>
    /// Called whenever the heartbeat frequency has elapsed.
    /// </summary>
    /// <remarks>
    /// Implementations should call <c>Call.KeepAliveAsync()</c> to keep the call active.
    /// </remarks>
    /// <param name="args">The elapsed event args.</param>
    /// <returns>A task representing the async operation.</returns>
    protected abstract Task HeartbeatAsync(ElapsedEventArgs args);

    /// <inheritdoc/>
    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _heartbeatTimer.Elapsed -= OnHeartbeatTimerElapsed;
            _heartbeatTimer.Stop();
            _heartbeatTimer.Dispose();
        }

        base.Dispose(disposing);
    }

    /// <summary>
    /// Internal heartbeat handler that invokes the async heartbeat method.
    /// </summary>
    private void OnHeartbeatTimerElapsed(object? sender, ElapsedEventArgs args)
    {
        var taskName = $"{GetType().FullName}.{nameof(HeartbeatAsync)}";
        GraphLogger.Verbose($"Starting heartbeat task: {taskName}");
        
        // Fire and forget, but log any exceptions
        _ = ExecuteHeartbeatAsync(args, taskName);
    }

    /// <summary>
    /// Executes the heartbeat asynchronously with error handling.
    /// </summary>
    private async Task ExecuteHeartbeatAsync(ElapsedEventArgs args, string taskName)
    {
        try
        {
            await HeartbeatAsync(args).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            GraphLogger.Error(ex, $"Heartbeat failed: {taskName}");
        }
    }
}
