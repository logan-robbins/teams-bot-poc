using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Publishes transcript events to Python FastAPI endpoint.
/// Uses snake_case JSON for Python compatibility.
/// 
/// Last Grunted: 01/31/2026 12:00:00 PM PST
/// </summary>
public sealed class PythonTranscriptPublisher : IDisposable
{
    private readonly string _endpoint;
    private readonly ILogger<PythonTranscriptPublisher> _logger;
    private readonly HttpClient _http;
    private readonly JsonSerializerOptions _jsonOptions;

    public PythonTranscriptPublisher(string endpoint, ILogger<PythonTranscriptPublisher> logger)
    {
        _endpoint = endpoint;
        _logger = logger;
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
        
        // Use snake_case for Python compatibility
        _jsonOptions = new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
            DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
        };
        
        _logger.LogInformation("Python transcript publisher initialized: {Endpoint}", endpoint);
    }

    public async Task PublishAsync(TranscriptEvent evt)
    {
        try
        {
            var response = await _http.PostAsJsonAsync(_endpoint, evt, _jsonOptions);
            
            if (!response.IsSuccessStatusCode)
            {
                _logger.LogWarning("Python endpoint returned {Status}", response.StatusCode);
            }
            else
            {
                _logger.LogDebug(
                    "Published transcript event: {EventType}, Text: {Text}",
                    evt.EventType,
                    evt.Text?.Length > 50 ? evt.Text[..50] + "..." : evt.Text);
            }
        }
        catch (TaskCanceledException)
        {
            _logger.LogWarning("Timeout publishing to Python endpoint");
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to publish to Python endpoint");
        }
    }

    public void Dispose()
    {
        _http.Dispose();
    }
}
