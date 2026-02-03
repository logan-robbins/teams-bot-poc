using Microsoft.AspNetCore.Mvc;
using Microsoft.Graph.Communications.Client;
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
    private readonly TranscriberFactory _transcriberFactory;
    private readonly ILogger<CallingController> _logger;

    public CallingController(
        TeamsCallingBotService botService,
        TranscriberFactory transcriberFactory,
        ILogger<CallingController> logger)
    {
        _botService = botService;
        _transcriberFactory = transcriberFactory;
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

            // Create a new transcriber for this call using the factory
            // The factory creates the transcriber outside of DI tracking,
            // so the DI container won't try to dispose it
            var transcriber = _transcriberFactory.Create();

            var callId = await _botService.JoinMeetingAsync(
                request.JoinUrl,
                request.DisplayName ?? "Talestral",
                request.JoinAsGuest,
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
            Service = "Talestral"
        });
    }
}

/// <summary>
/// Controller for Bot Framework messaging (stub - this bot is calling-only)
/// This prevents 404 errors when Azure Bot sends messaging probes
/// </summary>
[ApiController]
[Route("api/messages")]
public class MessagesController : ControllerBase
{
    private readonly ILogger<MessagesController> _logger;

    public MessagesController(ILogger<MessagesController> logger)
    {
        _logger = logger;
    }

    /// <summary>
    /// Bot Framework messaging endpoint (stub)
    /// This bot is calling-only, so we just acknowledge the request
    /// </summary>
    [HttpPost]
    public IActionResult Post()
    {
        _logger.LogInformation("Received Bot Framework message (this is a calling-only bot) - v1.0.2");
        return Ok(new { status = "ok", version = "1.0.2", message = "This bot handles calls, not messages" });
    }

    /// <summary>
    /// Health check for messages endpoint
    /// </summary>
    [HttpGet]
    public IActionResult Get()
    {
        return Ok(new { status = "ok", version = "1.0.2", endpoint = "messages", note = "This is a calling-only bot" });
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

    [JsonProperty("joinAsGuest")]
    public bool JoinAsGuest { get; set; } = false;
}

/// <summary>
/// Controller for Teams configurable tab configuration page
/// This is loaded when users try to add the bot/tab to a meeting
/// </summary>
[ApiController]
[Route("")]
public class ConfigureController : ControllerBase
{
    private readonly ILogger<ConfigureController> _logger;

    public ConfigureController(ILogger<ConfigureController> logger)
    {
        _logger = logger;
    }

    /// <summary>
    /// Configuration page for Teams configurable tab
    /// Returns HTML that Teams displays in the configuration modal
    /// </summary>
    [HttpGet("configure")]
    public IActionResult Configure()
    {
        _logger.LogInformation("Configuration page requested");

        var html = @"<!DOCTYPE html>
<html>
<head>
    <meta charset=""utf-8"">
    <meta name=""viewport"" content=""width=device-width, initial-scale=1.0"">
    <title>Talestral Configuration</title>
    <script src=""https://res.cdn.office.net/teams-js/2.0.0/js/MicrosoftTeams.min.js"" crossorigin=""anonymous""></script>
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
    <div class=""container"">
        <h1>Talestral</h1>
        <p>This bot automatically joins Teams meetings to provide real-time audio transcription.</p>
        
        <div class=""info-box"">
            <strong>About:</strong>
            <ul>
                <li>Automatically joins meetings when added</li>
                <li>Transcribes audio in real-time</li>
                <li>Transcripts are saved to the meeting organizer's desktop</li>
            </ul>
        </div>

        <p><strong>Note:</strong> This is a calling bot. It will join the meeting as a participant to transcribe audio.</p>
        
        <button class=""button"" id=""saveButton"" onclick=""save()"">Save</button>
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
</html>";

        return Content(html, "text/html");
    }
}
