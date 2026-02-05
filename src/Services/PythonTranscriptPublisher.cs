using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Publishes transcript events to Python FastAPI endpoint via HTTP POST.
/// </summary>
/// <remarks>
/// <para>
/// Uses snake_case JSON serialization for Python compatibility.
/// </para>
/// <para>
/// Note: This class creates its own HttpClient instance rather than using IHttpClientFactory
/// because it's instantiated via <see cref="TranscriberFactory"/> outside of DI container.
/// The HttpClient is configured with appropriate timeout and connection settings.
/// </para>
/// </remarks>
public sealed class PythonTranscriptPublisher : IDisposable
{
    /// <summary>
    /// Maximum length of text to include in log messages.
    /// </summary>
    private const int MaxLogTextLength = 50;
    
    /// <summary>
    /// HTTP request timeout in seconds.
    /// </summary>
    private const int TimeoutSeconds = 5;
    
    private readonly string _endpoint;
    private readonly ILogger<PythonTranscriptPublisher> _logger;
    private readonly HttpClient _httpClient;
    private readonly JsonSerializerOptions _jsonOptions;
    private bool _isDisposed;

    /// <summary>
    /// Initializes a new instance of the <see cref="PythonTranscriptPublisher"/> class.
    /// </summary>
    /// <param name="endpoint">The Python FastAPI endpoint URL.</param>
    /// <param name="logger">The logger instance.</param>
    /// <exception cref="ArgumentNullException">Thrown when required parameters are null.</exception>
    public PythonTranscriptPublisher(string endpoint, ILogger<PythonTranscriptPublisher> logger)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(endpoint);
        ArgumentNullException.ThrowIfNull(logger);
        
        _endpoint = endpoint;
        _logger = logger;
        
        // Configure HttpClient with appropriate settings
        // Note: Using SocketsHttpHandler for better connection pooling
        var handler = new SocketsHttpHandler
        {
            // Force DNS refresh every 15 minutes to handle DNS changes
            PooledConnectionLifetime = TimeSpan.FromMinutes(15),
            // Keep connections alive for reuse
            PooledConnectionIdleTimeout = TimeSpan.FromMinutes(2),
            // Limit concurrent connections per endpoint
            MaxConnectionsPerServer = 10
        };
        
        _httpClient = new HttpClient(handler)
        {
            Timeout = TimeSpan.FromSeconds(TimeoutSeconds)
        };
        
        // Use snake_case for Python compatibility
        _jsonOptions = new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
            DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
        };
        
        _logger.LogInformation("Python transcript publisher initialized: {Endpoint}", endpoint);
    }

    /// <summary>
    /// Publishes a transcript event to the Python endpoint.
    /// </summary>
    /// <param name="transcriptEvent">The transcript event to publish.</param>
    /// <param name="cancellationToken">Optional cancellation token.</param>
    /// <returns>A task representing the asynchronous operation.</returns>
    public async Task PublishAsync(TranscriptEvent transcriptEvent, CancellationToken cancellationToken = default)
    {
        ObjectDisposedException.ThrowIf(_isDisposed, this);
        ArgumentNullException.ThrowIfNull(transcriptEvent);
        
        try
        {
            using var response = await _httpClient
                .PostAsJsonAsync(_endpoint, transcriptEvent, _jsonOptions, cancellationToken)
                .ConfigureAwait(false);
            
            if (!response.IsSuccessStatusCode)
            {
                _logger.LogWarning(
                    "Python endpoint returned non-success status: {StatusCode} {ReasonPhrase}",
                    (int)response.StatusCode,
                    response.ReasonPhrase);
            }
            else
            {
                var truncatedText = TruncateText(transcriptEvent.Text);
                _logger.LogDebug(
                    "Published transcript event: EventType={EventType}, Text={Text}",
                    transcriptEvent.EventType,
                    truncatedText);
            }
        }
        catch (TaskCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            _logger.LogWarning(
                "Timeout publishing to Python endpoint after {Timeout}s",
                TimeoutSeconds);
        }
        catch (TaskCanceledException)
        {
            // Cancellation was requested - don't log as warning
            _logger.LogDebug("Publish operation was cancelled");
        }
        catch (HttpRequestException ex)
        {
            _logger.LogWarning(ex, "HTTP error publishing to Python endpoint: {Message}", ex.Message);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to publish to Python endpoint");
        }
    }

    /// <summary>
    /// Truncates text for logging purposes.
    /// </summary>
    private static string? TruncateText(string? text)
    {
        if (text is null)
        {
            return null;
        }
        
        return text.Length > MaxLogTextLength 
            ? string.Concat(text.AsSpan(0, MaxLogTextLength), "...") 
            : text;
    }

    /// <inheritdoc/>
    public void Dispose()
    {
        if (_isDisposed)
        {
            return;
        }

        _isDisposed = true;
        _httpClient.Dispose();
    }
}
