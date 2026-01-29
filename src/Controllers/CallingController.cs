using Microsoft.AspNetCore.Mvc;
using Microsoft.Graph.Communications.Common.Transport;
using Newtonsoft.Json;
using TeamsMediaBot.Services;

namespace TeamsMediaBot.Controllers;

/// <summary>
/// Controller for handling Teams calling webhook events
/// Per S1, S2: Bot must expose HTTPS webhook for calling notifications
/// </summary>
[ApiController]
[Route("api/[controller]")]
public class CallingController : ControllerBase
{
    private readonly TeamsCallingBotService _botService;
    private readonly IServiceProvider _serviceProvider;
    private readonly ILogger<CallingController> _logger;

    public CallingController(
        TeamsCallingBotService botService,
        IServiceProvider serviceProvider,
        ILogger<CallingController> logger)
    {
        _botService = botService;
        _serviceProvider = serviceProvider;
        _logger = logger;
    }

    /// <summary>
    /// Webhook endpoint for Graph calling notifications
    /// Per S1, S2: This is where Teams sends call state changes
    /// CRITICAL: Must call ProcessNotificationAsync to trigger SDK events
    /// </summary>
    [HttpPost]
    public async Task<IActionResult> OnIncomingRequest()
    {
        try
        {
            _logger.LogDebug("Received calling webhook: {Method} {Path}", Request.Method, Request.Path);

            // Convert ASP.NET Core HttpRequest to HttpRequestMessage
            var httpRequestMessage = ConvertToHttpRequestMessage(Request);

            // Pass notification to SDK for processing
            // This is REQUIRED to trigger call state events (OnUpdated, etc.)
            var response = await _botService.Client.ProcessNotificationAsync(httpRequestMessage)
                .ConfigureAwait(false);

            _logger.LogDebug("Notification processed, status: {StatusCode}", response.StatusCode);

            // Convert HttpResponseMessage back to IActionResult
            return new HttpResponseMessageResult(response);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error processing calling webhook");
            return StatusCode(500);
        }
    }

    /// <summary>
    /// Convert ASP.NET Core HttpRequest to System.Net.Http.HttpRequestMessage
    /// Required for Graph Communications SDK compatibility
    /// </summary>
    private static HttpRequestMessage ConvertToHttpRequestMessage(HttpRequest request)
    {
        var httpRequestMessage = new HttpRequestMessage
        {
            Method = new HttpMethod(request.Method),
            RequestUri = new UriBuilder
            {
                Scheme = request.Scheme,
                Host = request.Host.Host,
                Port = request.Host.Port ?? (request.Scheme == "https" ? 443 : 80),
                Path = request.PathBase.Add(request.Path),
                Query = request.QueryString.ToString()
            }.Uri,
            Content = new StreamContent(request.Body)
        };

        // Copy headers
        foreach (var header in request.Headers)
        {
            if (!httpRequestMessage.Headers.TryAddWithoutValidation(header.Key, header.Value.AsEnumerable()))
            {
                httpRequestMessage.Content?.Headers.TryAddWithoutValidation(header.Key, header.Value.AsEnumerable());
            }
        }

        // Set content type if present
        if (request.ContentType != null)
        {
            httpRequestMessage.Content!.Headers.ContentType = 
                System.Net.Http.Headers.MediaTypeHeaderValue.Parse(request.ContentType);
        }

        return httpRequestMessage;
    }

    /// <summary>
    /// Endpoint to manually trigger bot to join a meeting
    /// Per D10: This is how we test the bot
    /// </summary>
    [HttpPost("join")]
    public async Task<IActionResult> JoinMeeting([FromBody] JoinMeetingRequest request)
    {
        try
        {
            _logger.LogInformation("Received join meeting request: {JoinUrl}", request.JoinUrl);

            // Create a new transcriber for this call
            using var scope = _serviceProvider.CreateScope();
            var transcriber = scope.ServiceProvider.GetRequiredService<AzureSpeechRealtimeTranscriber>();

            var callId = await _botService.JoinMeetingAsync(
                request.JoinUrl,
                request.DisplayName ?? "Transcription Bot",
                transcriber);

            return Ok(new
            {
                CallId = callId,
                Message = "Bot is joining the meeting",
                JoinUrl = request.JoinUrl
            });
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to join meeting");
            return StatusCode(500, new { Error = ex.Message });
        }
    }

    /// <summary>
    /// Health check endpoint
    /// </summary>
    [HttpGet("health")]
    public IActionResult Health()
    {
        return Ok(new
        {
            Status = "Healthy",
            Timestamp = DateTime.UtcNow,
            Service = "Teams Media Bot POC"
        });
    }
}

/// <summary>
/// Helper class to convert HttpResponseMessage to IActionResult
/// Required because Graph SDK returns HttpResponseMessage but ASP.NET Core uses IActionResult
/// </summary>
internal class HttpResponseMessageResult : IActionResult
{
    private readonly HttpResponseMessage _responseMessage;

    public HttpResponseMessageResult(HttpResponseMessage responseMessage)
    {
        _responseMessage = responseMessage;
    }

    public async Task ExecuteResultAsync(ActionContext context)
    {
        var response = context.HttpContext.Response;

        response.StatusCode = (int)_responseMessage.StatusCode;

        // Copy headers
        foreach (var header in _responseMessage.Headers)
        {
            response.Headers[header.Key] = header.Value.ToArray();
        }

        // Copy content headers
        if (_responseMessage.Content != null)
        {
            foreach (var header in _responseMessage.Content.Headers)
            {
                response.Headers[header.Key] = header.Value.ToArray();
            }

            // Copy content
            await _responseMessage.Content.CopyToAsync(response.Body);
        }
    }
}

/// <summary>
/// Request model for joining meetings
/// </summary>
public class JoinMeetingRequest
{
    [JsonProperty("joinUrl")]
    public required string JoinUrl { get; set; }

    [JsonProperty("displayName")]
    public string? DisplayName { get; set; }
}
