using Microsoft.AspNetCore.Mvc;
using Microsoft.Graph.Communications.Client;
using Newtonsoft.Json;
using TeamsMediaBot.Services;

namespace TeamsMediaBot.Controllers;

/// <summary>
/// Controller for handling Microsoft Teams calling webhook events.
/// </summary>
/// <remarks>
/// <para>
/// Per Microsoft Graph Communications SDK documentation:
/// Bot must expose HTTPS webhook for calling notifications from Microsoft Graph.
/// </para>
/// <para>
/// All webhook notifications are processed through ProcessNotificationAsync to trigger SDK events.
/// </para>
/// </remarks>
[ApiController]
[Route("api/[controller]")]
public class CallingController : ControllerBase
{
    private readonly TeamsCallingBotService _botService;
    private readonly TranscriberFactory _transcriberFactory;
    private readonly ILogger<CallingController> _logger;

    /// <summary>
    /// Initializes a new instance of the <see cref="CallingController"/> class.
    /// </summary>
    /// <param name="botService">The Teams calling bot service.</param>
    /// <param name="transcriberFactory">The factory for creating transcribers.</param>
    /// <param name="logger">The logger instance.</param>
    public CallingController(
        TeamsCallingBotService botService,
        TranscriberFactory transcriberFactory,
        ILogger<CallingController> logger)
    {
        _botService = botService ?? throw new ArgumentNullException(nameof(botService));
        _transcriberFactory = transcriberFactory ?? throw new ArgumentNullException(nameof(transcriberFactory));
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
    }

    /// <summary>
    /// Webhook endpoint for Graph Communications calling notifications.
    /// </summary>
    /// <remarks>
    /// <para>
    /// This endpoint receives call state change notifications from Microsoft Graph.
    /// </para>
    /// <para>
    /// CRITICAL: Must call ProcessNotificationAsync to trigger SDK events (OnUpdated, etc.).
    /// The SDK handles all the complex state management internally.
    /// </para>
    /// </remarks>
    /// <returns>The response from the Graph Communications SDK.</returns>
    [HttpPost]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status500InternalServerError)]
    public async Task<IActionResult> OnIncomingRequestAsync(CancellationToken cancellationToken)
    {
        try
        {
            _logger.LogDebug(
                "Received calling webhook: {Method} {Path}",
                Request.Method,
                Request.Path);

            // Convert ASP.NET Core HttpRequest to HttpRequestMessage
            var httpRequestMessage = ConvertToHttpRequestMessage(Request);

            // Pass notification to SDK for processing
            // This is REQUIRED to trigger call state events (OnUpdated, etc.)
            var response = await _botService.Client
                .ProcessNotificationAsync(httpRequestMessage)
                .ConfigureAwait(false);

            _logger.LogDebug(
                "Notification processed, status: {StatusCode}",
                response.StatusCode);

            // Convert HttpResponseMessage back to IActionResult
            return new HttpResponseMessageResult(response);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error processing calling webhook");
            return StatusCode(StatusCodes.Status500InternalServerError);
        }
    }

    /// <summary>
    /// Converts ASP.NET Core HttpRequest to System.Net.Http.HttpRequestMessage.
    /// </summary>
    /// <remarks>
    /// Required for Graph Communications SDK compatibility as the SDK expects
    /// HttpRequestMessage but ASP.NET Core provides HttpRequest.
    /// </remarks>
    private static HttpRequestMessage ConvertToHttpRequestMessage(HttpRequest request)
    {
        var httpRequestMessage = new HttpRequestMessage
        {
            Method = new HttpMethod(request.Method),
            RequestUri = new UriBuilder
            {
                Scheme = request.Scheme,
                Host = request.Host.Host,
                Port = request.Host.Port ?? (string.Equals(request.Scheme, "https", StringComparison.OrdinalIgnoreCase) ? 443 : 80),
                Path = request.PathBase.Add(request.Path),
                Query = request.QueryString.ToString()
            }.Uri,
            Content = new StreamContent(request.Body)
        };

        // Copy headers from ASP.NET Core request to HttpRequestMessage
        foreach (var header in request.Headers)
        {
            if (!httpRequestMessage.Headers.TryAddWithoutValidation(header.Key, header.Value.AsEnumerable()))
            {
                httpRequestMessage.Content?.Headers.TryAddWithoutValidation(header.Key, header.Value.AsEnumerable());
            }
        }

        // Set content type if present
        if (request.ContentType is not null)
        {
            httpRequestMessage.Content!.Headers.ContentType = 
                System.Net.Http.Headers.MediaTypeHeaderValue.Parse(request.ContentType);
        }

        return httpRequestMessage;
    }

    /// <summary>
    /// Manually triggers the bot to join a Teams meeting.
    /// </summary>
    /// <param name="request">The join meeting request containing the meeting URL.</param>
    /// <param name="cancellationToken">Cancellation token.</param>
    /// <returns>Information about the initiated call.</returns>
    [HttpPost("join")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status400BadRequest)]
    [ProducesResponseType(StatusCodes.Status500InternalServerError)]
    public async Task<IActionResult> JoinMeetingAsync(
        [FromBody] JoinMeetingRequest request,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(request.JoinUrl))
        {
            return BadRequest(new { Error = "JoinUrl is required" });
        }

        try
        {
            _logger.LogInformation("Received join meeting request: {JoinUrl}", request.JoinUrl);

            // Create a new transcriber for this call using the factory
            // The factory creates the transcriber outside of DI tracking,
            // so the DI container won't try to dispose it
            var transcriber = _transcriberFactory.Create();

            var callId = await _botService.JoinMeetingAsync(
                request.JoinUrl,
                request.DisplayName ?? "Talestral",
                request.JoinAsGuest,
                transcriber)
                .ConfigureAwait(false);

            return Ok(new JoinMeetingResponse
            {
                CallId = callId,
                Message = "Bot is joining the meeting",
                JoinUrl = request.JoinUrl
            });
        }
        catch (InvalidOperationException ex)
        {
            _logger.LogWarning(ex, "Invalid join meeting request: {JoinUrl}", request.JoinUrl);
            return BadRequest(new { Error = ex.Message });
        }
        catch (ArgumentException ex)
        {
            _logger.LogWarning(ex, "Invalid join URL: {JoinUrl}", request.JoinUrl);
            return BadRequest(new { Error = ex.Message });
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to join meeting: {JoinUrl}", request.JoinUrl);
            return StatusCode(
                StatusCodes.Status500InternalServerError, 
                new { Error = "Failed to join meeting. See server logs for details." });
        }
    }

    /// <summary>
    /// Health check endpoint for the calling service.
    /// </summary>
    /// <returns>Health status with timestamp.</returns>
    [HttpGet("health")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    public IActionResult GetHealth()
    {
        return Ok(new HealthCheckResponse
        {
            Status = "Healthy",
            TimestampUtc = DateTime.UtcNow,
            Service = "Talestral",
            ActiveCalls = _botService.CallHandlers.Count
        });
    }
}

/// <summary>
/// Controller for Bot Framework messaging endpoint (stub - this bot is calling-only).
/// </summary>
/// <remarks>
/// This prevents 404 errors when Azure Bot Service sends messaging probes.
/// The bot does not handle messages, only calls.
/// </remarks>
[ApiController]
[Route("api/messages")]
public class MessagesController : ControllerBase
{
    private const string BotVersion = "1.0.2";
    
    private readonly ILogger<MessagesController> _logger;

    /// <summary>
    /// Initializes a new instance of the <see cref="MessagesController"/> class.
    /// </summary>
    public MessagesController(ILogger<MessagesController> logger)
    {
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
    }

    /// <summary>
    /// Bot Framework messaging endpoint (stub).
    /// </summary>
    /// <remarks>
    /// This bot is calling-only, so we just acknowledge the request
    /// without processing any messages.
    /// </remarks>
    /// <returns>Acknowledgment response.</returns>
    [HttpPost]
    [ProducesResponseType(StatusCodes.Status200OK)]
    public IActionResult PostMessage()
    {
        _logger.LogInformation(
            "Received Bot Framework message (this is a calling-only bot) - v{Version}",
            BotVersion);
        
        return Ok(new MessagesResponse
        {
            Status = "ok",
            Version = BotVersion,
            Message = "This bot handles calls, not messages"
        });
    }

    /// <summary>
    /// Health check for the messages endpoint.
    /// </summary>
    /// <returns>Endpoint status.</returns>
    [HttpGet]
    [ProducesResponseType(StatusCodes.Status200OK)]
    public IActionResult GetStatus()
    {
        return Ok(new MessagesResponse
        {
            Status = "ok",
            Version = BotVersion,
            Endpoint = "messages",
            Note = "This is a calling-only bot"
        });
    }
}

/// <summary>
/// Helper class to convert HttpResponseMessage to IActionResult.
/// </summary>
/// <remarks>
/// Required because Graph Communications SDK returns HttpResponseMessage 
/// but ASP.NET Core uses IActionResult.
/// </remarks>
internal sealed class HttpResponseMessageResult : IActionResult
{
    private readonly HttpResponseMessage _responseMessage;

    /// <summary>
    /// Initializes a new instance of the <see cref="HttpResponseMessageResult"/> class.
    /// </summary>
    public HttpResponseMessageResult(HttpResponseMessage responseMessage)
    {
        _responseMessage = responseMessage ?? throw new ArgumentNullException(nameof(responseMessage));
    }

    /// <inheritdoc/>
    public async Task ExecuteResultAsync(ActionContext context)
    {
        ArgumentNullException.ThrowIfNull(context);
        
        var response = context.HttpContext.Response;
        response.StatusCode = (int)_responseMessage.StatusCode;

        // Copy response headers
        foreach (var header in _responseMessage.Headers)
        {
            response.Headers[header.Key] = header.Value.ToArray();
        }

        // Copy content headers and body
        if (_responseMessage.Content is not null)
        {
            foreach (var header in _responseMessage.Content.Headers)
            {
                response.Headers[header.Key] = header.Value.ToArray();
            }

            await _responseMessage.Content
                .CopyToAsync(response.Body)
                .ConfigureAwait(false);
        }
    }
}

#region Request/Response Models

/// <summary>
/// Request model for joining Teams meetings.
/// </summary>
public sealed class JoinMeetingRequest
{
    /// <summary>
    /// Gets or sets the Teams meeting join URL.
    /// </summary>
    [JsonProperty("joinUrl")]
    public required string JoinUrl { get; set; }

    /// <summary>
    /// Gets or sets the display name for the bot in the meeting.
    /// </summary>
    [JsonProperty("displayName")]
    public string? DisplayName { get; set; }

    /// <summary>
    /// Gets or sets whether to join as a guest (with display name) or as the app identity.
    /// </summary>
    [JsonProperty("joinAsGuest")]
    public bool JoinAsGuest { get; set; }
}

/// <summary>
/// Response model for join meeting requests.
/// </summary>
public sealed class JoinMeetingResponse
{
    /// <summary>
    /// Gets or sets the call ID assigned by Graph Communications SDK.
    /// </summary>
    public required string CallId { get; init; }

    /// <summary>
    /// Gets or sets a human-readable status message.
    /// </summary>
    public required string Message { get; init; }

    /// <summary>
    /// Gets or sets the join URL that was used.
    /// </summary>
    public required string JoinUrl { get; init; }
}

/// <summary>
/// Response model for health check endpoint.
/// </summary>
public sealed class HealthCheckResponse
{
    /// <summary>
    /// Gets or sets the health status.
    /// </summary>
    public required string Status { get; init; }

    /// <summary>
    /// Gets or sets the UTC timestamp of the health check.
    /// </summary>
    public required DateTime TimestampUtc { get; init; }

    /// <summary>
    /// Gets or sets the service name.
    /// </summary>
    public required string Service { get; init; }

    /// <summary>
    /// Gets or sets the number of active calls.
    /// </summary>
    public int ActiveCalls { get; init; }
}

/// <summary>
/// Response model for messages endpoint.
/// </summary>
public sealed class MessagesResponse
{
    /// <summary>
    /// Gets or sets the status.
    /// </summary>
    public required string Status { get; init; }

    /// <summary>
    /// Gets or sets the bot version.
    /// </summary>
    public required string Version { get; init; }

    /// <summary>
    /// Gets or sets an optional message.
    /// </summary>
    public string? Message { get; init; }

    /// <summary>
    /// Gets or sets the endpoint name.
    /// </summary>
    public string? Endpoint { get; init; }

    /// <summary>
    /// Gets or sets an optional note.
    /// </summary>
    public string? Note { get; init; }
}

#endregion

/// <summary>
/// Controller for Teams configurable tab configuration page.
/// </summary>
/// <remarks>
/// This is loaded when users try to add the bot/tab to a Teams meeting.
/// Returns an HTML page that Teams displays in the configuration modal.
/// </remarks>
[ApiController]
[Route("")]
public class ConfigureController : ControllerBase
{
    private readonly ILogger<ConfigureController> _logger;

    /// <summary>
    /// Initializes a new instance of the <see cref="ConfigureController"/> class.
    /// </summary>
    public ConfigureController(ILogger<ConfigureController> logger)
    {
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
    }

    /// <summary>
    /// Returns the configuration page HTML for the Teams configurable tab.
    /// </summary>
    /// <returns>HTML content for the configuration modal.</returns>
    [HttpGet("configure")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [Produces("text/html")]
    public IActionResult GetConfigurationPage()
    {
        _logger.LogInformation("Configuration page requested");

        // Note: This HTML is embedded for simplicity. In production, consider
        // serving static files or using Razor pages for better maintainability.
        const string html = """
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Talestral Configuration</title>
                <script src="https://res.cdn.office.net/teams-js/2.0.0/js/MicrosoftTeams.min.js" crossorigin="anonymous"></script>
                <style>
                    body {
                        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                        padding: 20px;
                        max-width: 600px;
                        margin: 0 auto;
                        background-color: #f5f5f5;
                    }
                    .container {
                        background: white;
                        padding: 30px;
                        border-radius: 8px;
                        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                    }
                    h1 {
                        color: #6264A7;
                        margin-top: 0;
                    }
                    p {
                        color: #333;
                        line-height: 1.6;
                    }
                    .info-box {
                        background: #E8F0FE;
                        border-left: 4px solid #6264A7;
                        padding: 15px;
                        margin: 20px 0;
                    }
                    .button {
                        background-color: #6264A7;
                        color: white;
                        border: none;
                        padding: 12px 24px;
                        border-radius: 4px;
                        cursor: pointer;
                        font-size: 14px;
                        margin-top: 20px;
                    }
                    .button:hover {
                        background-color: #5051A3;
                    }
                    .button:disabled {
                        background-color: #ccc;
                        cursor: not-allowed;
                    }
                </style>
            </head>
            <body>
                <div class="container">
                    <h1>Talestral</h1>
                    <p>This bot automatically joins Teams meetings to provide real-time audio transcription.</p>
                    
                    <div class="info-box">
                        <strong>About:</strong>
                        <ul>
                            <li>Automatically joins meetings when added</li>
                            <li>Transcribes audio in real-time</li>
                            <li>Transcripts are saved to the meeting organizer's desktop</li>
                        </ul>
                    </div>

                    <p><strong>Note:</strong> This is a calling bot. It will join the meeting as a participant to transcribe audio.</p>
                    
                    <button class="button" id="saveButton" onclick="save()">Save</button>
                </div>

                <script>
                    microsoftTeams.app.initialize().then(() => {
                        // Enable the save button once Teams SDK is ready
                        document.getElementById('saveButton').disabled = false;
                        microsoftTeams.settings.setValidityState(true);
                    });

                    function save() {
                        // Save configuration (no settings needed for this bot)
                        microsoftTeams.settings.setSettings({
                            contentUrl: window.location.origin + '/configure',
                            suggestedDisplayName: 'Talestral',
                            websiteUrl: window.location.origin
                        });
                        
                        // Notify Teams that settings are saved
                        microsoftTeams.settings.setValidityState(true);
                        microsoftTeams.settings.save();
                    }
                </script>
            </body>
            </html>
            """;

        return Content(html, "text/html");
    }
}
