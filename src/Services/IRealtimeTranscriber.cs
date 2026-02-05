namespace TeamsMediaBot.Services;

/// <summary>
/// Abstraction for real-time speech-to-text implementations.
/// </summary>
/// <remarks>
/// <para>
/// Keeps the call/media pipeline independent of any single STT provider.
/// Implementations include Deepgram (primary) and Azure Speech (fallback).
/// </para>
/// <para>
/// Audio format: 16kHz, 16-bit, mono PCM (640 bytes per 20ms frame).
/// </para>
/// </remarks>
public interface IRealtimeTranscriber : IAsyncDisposable
{
    /// <summary>
    /// Starts the transcription session.
    /// </summary>
    /// <param name="ct">Optional cancellation token.</param>
    /// <returns>A task representing the asynchronous start operation.</returns>
    Task StartAsync(CancellationToken ct = default);
    
    /// <summary>
    /// Pushes a PCM audio frame to the transcriber.
    /// </summary>
    /// <remarks>
    /// Expected format: 16kHz sample rate, 16-bit signed, mono channel.
    /// Teams Media SDK delivers 20ms frames (640 bytes each).
    /// This method must be non-blocking as it's called from the audio receive callback.
    /// </remarks>
    /// <param name="pcmFrame">The PCM audio data.</param>
    void PushPcm16k16bitMono(ReadOnlySpan<byte> pcmFrame);
    
    /// <summary>
    /// Stops the transcription session.
    /// </summary>
    /// <returns>A task representing the asynchronous stop operation.</returns>
    Task StopAsync();
}

