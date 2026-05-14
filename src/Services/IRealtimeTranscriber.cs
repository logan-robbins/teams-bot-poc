using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Abstraction for real-time speech-to-text implementations.
/// </summary>
/// <remarks>
/// Audio format: 16kHz, 16-bit, mono PCM (640 bytes per 20ms frame).
/// </remarks>
public interface IRealtimeTranscriber : IAsyncDisposable
{
    /// <summary>
    /// Meeting reference stamped on every emitted transcript envelope.
    /// Set by the join workflow after construction once the meeting id is known.
    /// </summary>
    MeetingRef? MeetingRef { get; set; }

    Task StartAsync(CancellationToken ct = default);

    void PushPcm16k16bitMono(ReadOnlySpan<byte> pcmFrame);

    Task StopAsync();
}
