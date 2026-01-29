using System.Net.Http.Json;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Publishes transcript events to Python agent via HTTP POST
/// Based on Part I (I2) of the validated guide
/// </summary>
public sealed class PythonTranscriptPublisher : IDisposable
{
    private readonly HttpClient _http = new();
    private readonly Uri _endpoint;
    private readonly ILogger<PythonTranscriptPublisher> _logger;

    public PythonTranscriptPublisher(
        string endpointUrl,
        ILogger<PythonTranscriptPublisher> logger)
    {
        _endpoint = new Uri(endpointUrl);
        _logger = logger;
        
        _logger.LogInformation("Python transcript publisher initialized: {Endpoint}", endpointUrl);
    }

    public async Task PublishAsync(TranscriptEvent evt, CancellationToken ct = default)
    {
        try
        {
            var response = await _http.PostAsJsonAsync(_endpoint, evt, ct);
            
            if (!response.IsSuccessStatusCode)
            {
                _logger.LogWarning(
                    "Failed to publish transcript event. Status: {Status}, Event: {Kind}",
                    response.StatusCode,
                    evt.Kind);
            }
            else
            {
                _logger.LogDebug(
                    "Published transcript event: {Kind}, Text: {Text}",
                    evt.Kind,
                    evt.Text?.Length > 50 ? evt.Text[..50] + "..." : evt.Text);
            }
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error publishing transcript event to Python endpoint");
            // Don't throw - we don't want transcription failures to crash the bot
        }
    }

    public void Dispose()
    {
        _http.Dispose();
    }
}
