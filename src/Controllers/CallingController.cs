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
    /// </summary>
    [HttpPost]
    public async Task<IActionResult> OnIncomingRequest()
    {
        try
        {
            // Read raw request body
            using var reader = new StreamReader(Request.Body);
            var body = await reader.ReadToEndAsync();

            _logger.LogDebug("Received calling webhook: {Body}", body);

            // The Graph Communications SDK processes these notifications internally
            // We just need to acknowledge receipt
            return Ok();
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error processing calling webhook");
            return StatusCode(500);
        }
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
/// Request model for joining meetings
/// </summary>
public class JoinMeetingRequest
{
    [JsonProperty("joinUrl")]
    public required string JoinUrl { get; set; }

    [JsonProperty("displayName")]
    public string? DisplayName { get; set; }
}
