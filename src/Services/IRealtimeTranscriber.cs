namespace TeamsMediaBot.Services;

/// <summary>
/// Abstraction for real-time speech-to-text implementations.
/// Keeps the call/media pipeline independent of any single STT provider.
/// </summary>
public interface IRealtimeTranscriber : IAsyncDisposable
{
    Task StartAsync(CancellationToken ct = default);
    void PushPcm16k16bitMono(ReadOnlySpan<byte> pcmFrame);
    Task StopAsync();
}

