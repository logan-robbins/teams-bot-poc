using Microsoft.AspNetCore.Mvc;
using Newtonsoft.Json;
using TeamsMediaBot.Models;
using TeamsMediaBot.Services;

namespace TeamsMediaBot.Controllers;

/// <summary>
/// Operator-facing API for managing Alfred's persistent channel attachments.
///
/// Channel attachment is the channel-level analog of "the bot is in this
/// meeting": once attached, Alfred listens to every message in the channel
/// (via a Graph change-notification subscription on
/// <c>teams/{teamId}/channels/{channelId}/messages</c>) and is allowed to
/// post back into it. Attachments survive bot restarts via
/// <see cref="ChannelAttachmentStore"/>.
/// </summary>
[ApiController]
[Route("api/channels")]
public sealed class ChannelAttachmentController : ControllerBase
{
    private readonly IChannelAttachmentService _service;
    private readonly BotConfiguration _botConfig;
    private readonly TeamsCallingBotService _botService;
    private readonly TranscriberFactory _transcriberFactory;
    private readonly OfficialTranscriptFetcher _transcriptFetcher;
    private readonly ILogger<ChannelAttachmentController> _logger;

    public ChannelAttachmentController(
        IChannelAttachmentService service,
        BotConfiguration botConfig,
        TeamsCallingBotService botService,
        TranscriberFactory transcriberFactory,
        OfficialTranscriptFetcher transcriptFetcher,
        ILogger<ChannelAttachmentController> logger)
    {
        _service = service;
        _botConfig = botConfig;
        _botService = botService;
        _transcriberFactory = transcriberFactory;
        _transcriptFetcher = transcriptFetcher;
        _logger = logger;
    }

    [HttpGet]
    [ProducesResponseType(StatusCodes.Status200OK)]
    public IActionResult List()
    {
        var items = _service.List()
            .Select(record => new ChannelAttachmentResponse
            {
                TeamId = record.TeamId,
                ChannelId = record.ChannelId,
                ConversationThreadId = record.ConversationThreadId,
                TeamDisplayName = record.TeamDisplayName,
                ChannelDisplayName = record.ChannelDisplayName,
                Source = record.Source,
                AttachedAtUtc = record.AttachedAtUtc,
                SubscriptionId = record.SubscriptionId,
                SubscriptionExpiresAtUtc = record.SubscriptionExpiresAtUtc,
            })
            .OrderBy(item => item.TeamId, StringComparer.Ordinal)
            .ThenBy(item => item.ChannelId, StringComparer.Ordinal)
            .ToList();

        return Ok(new { count = items.Count, attachments = items });
    }

    [HttpPost("attach")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status400BadRequest)]
    [ProducesResponseType(StatusCodes.Status500InternalServerError)]
    public async Task<IActionResult> Attach(
        [FromBody] AttachChannelRequest request,
        CancellationToken cancellationToken)
    {
        if (request is null
            || string.IsNullOrWhiteSpace(request.TeamId)
            || string.IsNullOrWhiteSpace(request.ChannelId))
        {
            return BadRequest(new { error = "team_id and channel_id are required" });
        }

        try
        {
            await _service.AttachAsync(
                new ChannelAttachmentRequest
                {
                    TeamId = request.TeamId.Trim(),
                    ChannelId = request.ChannelId.Trim(),
                    ConversationThreadId = string.IsNullOrWhiteSpace(request.ConversationThreadId)
                        ? null
                        : request.ConversationThreadId.Trim(),
                    ChannelDisplayName = request.ChannelDisplayName,
                    TeamDisplayName = request.TeamDisplayName,
                    ServiceUrl = request.ServiceUrl,
                    TenantId = request.TenantId,
                    Source = string.IsNullOrWhiteSpace(request.Source) ? "manual_attach" : request.Source,
                },
                cancellationToken);
        }
        catch (InvalidOperationException ex)
        {
            _logger.LogWarning(
                ex,
                "Channel attach prerequisites missing TeamId={TeamId} ChannelId={ChannelId}",
                request.TeamId,
                request.ChannelId);
            return StatusCode(StatusCodes.Status500InternalServerError, new { error = ex.Message });
        }
        catch (Exception ex)
        {
            _logger.LogError(
                ex,
                "Failed to attach channel TeamId={TeamId} ChannelId={ChannelId}",
                request.TeamId,
                request.ChannelId);
            return StatusCode(StatusCodes.Status500InternalServerError, new { error = ex.Message });
        }

        var record = _service.Get(request.TeamId, request.ChannelId);
        return Ok(new
        {
            ok = true,
            attachment = record is null ? null : new ChannelAttachmentResponse
            {
                TeamId = record.TeamId,
                ChannelId = record.ChannelId,
                ConversationThreadId = record.ConversationThreadId,
                TeamDisplayName = record.TeamDisplayName,
                ChannelDisplayName = record.ChannelDisplayName,
                Source = record.Source,
                AttachedAtUtc = record.AttachedAtUtc,
                SubscriptionId = record.SubscriptionId,
                SubscriptionExpiresAtUtc = record.SubscriptionExpiresAtUtc,
            },
        });
    }

    [HttpDelete("{teamId}/{channelId}")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<IActionResult> Detach(
        string teamId,
        string channelId,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(teamId) || string.IsNullOrWhiteSpace(channelId))
        {
            return BadRequest(new { error = "team_id and channel_id are required" });
        }

        var removed = await _service.DetachAsync(teamId, channelId, cancellationToken);
        if (!removed)
        {
            return NotFound(new { error = "no attachment found for that team_id + channel_id" });
        }

        return Ok(new { ok = true, detached = true });
    }

    [HttpGet("{teamId}/{channelId}/consumers")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public IActionResult ListConsumers(string teamId, string channelId)
    {
        var record = _service.Get(teamId, channelId);
        if (record is null)
        {
            return NotFound(new { error = "no attachment for that team_id + channel_id" });
        }

        return Ok(new
        {
            team_id = record.TeamId,
            channel_id = record.ChannelId,
            consumers = record.Consumers,
        });
    }

    [HttpPut("{teamId}/{channelId}/consumers")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status400BadRequest)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<IActionResult> ReplaceConsumers(
        string teamId,
        string channelId,
        [FromBody] ReplaceConsumersRequest request,
        CancellationToken cancellationToken)
    {
        if (request?.Consumers is null)
        {
            return BadRequest(new { error = "consumers array is required" });
        }

        try
        {
            var ok = await _service.SetConsumersAsync(
                teamId, channelId, request.Consumers, cancellationToken);
            if (!ok)
            {
                return NotFound(new { error = "no attachment for that team_id + channel_id" });
            }
        }
        catch (InvalidOperationException ex)
        {
            return BadRequest(new { error = ex.Message });
        }

        var record = _service.Get(teamId, channelId)!;
        return Ok(new { ok = true, consumers = record.Consumers });
    }

    [HttpPost("{teamId}/{channelId}/consumers")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status400BadRequest)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<IActionResult> UpsertConsumer(
        string teamId,
        string channelId,
        [FromBody] ConsumerConfig consumer,
        CancellationToken cancellationToken)
    {
        if (consumer is null)
        {
            return BadRequest(new { error = "consumer body is required" });
        }

        try
        {
            var ok = await _service.UpsertConsumerAsync(
                teamId, channelId, consumer, cancellationToken);
            if (!ok)
            {
                return NotFound(new { error = "no attachment for that team_id + channel_id" });
            }
        }
        catch (InvalidOperationException ex)
        {
            return BadRequest(new { error = ex.Message });
        }

        var record = _service.Get(teamId, channelId)!;
        return Ok(new { ok = true, consumers = record.Consumers });
    }

    [HttpDelete("{teamId}/{channelId}/consumers/{consumerName}")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<IActionResult> RemoveConsumer(
        string teamId,
        string channelId,
        string consumerName,
        CancellationToken cancellationToken)
    {
        var ok = await _service.RemoveConsumerAsync(
            teamId, channelId, consumerName, cancellationToken);
        if (!ok)
        {
            return NotFound(new { error = "no consumer by that name (or no attachment for that channel)" });
        }
        return Ok(new { ok = true });
    }

    /// <summary>
    /// Toggles <c>auto_join_enabled</c> on the attachment record. When
    /// false, the bot stops auto-joining channel meetings on
    /// <c>callStartedEventMessageDetail</c>; operators must use the
    /// manual <c>/join</c> endpoint instead.
    /// </summary>
    [HttpPatch("{teamId}/{channelId}/auto-join")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status400BadRequest)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<IActionResult> SetAutoJoin(
        string teamId,
        string channelId,
        [FromBody] SetAutoJoinRequest request,
        CancellationToken cancellationToken)
    {
        if (request?.Enabled is null)
        {
            return BadRequest(new { error = "enabled (bool) is required" });
        }

        var ok = await _service.SetAutoJoinAsync(
            teamId, channelId, request.Enabled.Value, cancellationToken);
        if (!ok)
        {
            return NotFound(new { error = "no attachment for that team_id + channel_id" });
        }

        var record = _service.Get(teamId, channelId)!;
        return Ok(new { ok = true, auto_join_enabled = record.AutoJoinEnabled });
    }

    /// <summary>
    /// Manually triggers Alfred to join the channel's current meeting
    /// room. Uses the same workflow as auto-join; synthesizes the
    /// channel-meeting join URL from the attachment's channel thread
    /// + the bot's tenant id. Returns the workflow result (call id,
    /// selected mode, etc.) or a structured error.
    /// </summary>
    [HttpPost("{teamId}/{channelId}/join")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status400BadRequest)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<IActionResult> JoinNow(
        string teamId,
        string channelId,
        CancellationToken cancellationToken)
    {
        var record = _service.Get(teamId, channelId);
        if (record is null)
        {
            return NotFound(new { error = "no attachment for that team_id + channel_id" });
        }

        var channelThreadId = record.ConversationThreadId;
        if (string.IsNullOrWhiteSpace(channelThreadId))
        {
            channelThreadId = $"19:{record.ChannelId}@thread.tacv2";
        }

        var tenantId = record.TenantId ?? _botConfig.TenantId;
        if (string.IsNullOrWhiteSpace(tenantId))
        {
            return BadRequest(new { error = "no tenant id on attachment or bot config" });
        }

        var joinUrl = ChannelMeetingJoinUrls.Build(
            channelThreadId!,
            tenantId!,
            _botConfig.AppId ?? string.Empty);

        try
        {
            var transcriber = _transcriberFactory.Create();
            var result = await _botService.JoinMeetingWithModeAsync(
                new JoinMeetingCommand
                {
                    JoinUrl = joinUrl,
                    DisplayName = "Alfred",
                    JoinAsGuest = false,
                    RequestedJoinMode = JoinModeNames.InviteAndGraphJoin,
                    OrganizerTenantId = tenantId,
                    BotAttendeePresent = true,
                },
                transcriber);

            _logger.LogInformation(
                "Manual join requested team={TeamId} channel={ChannelId} result.CallId={CallId} mode={Mode}",
                teamId, channelId, result.CallId, result.SelectedJoinMode);

            // No organizer OID on manual trigger (no systemEventMessage in
            // hand). Skip post-meeting transcript fetch — auto-join path
            // wires it from initiator. Operators wanting the official
            // transcript for a manual-trigger call should fetch via Graph
            // directly using the meeting organizer's userId.
            return Ok(new
            {
                ok = true,
                call_id = result.CallId,
                join_mode = result.SelectedJoinMode,
                deferred = result.Deferred,
                message = result.Message,
                join_url = joinUrl,
            });
        }
        catch (JoinWorkflowException ex)
        {
            _logger.LogWarning(ex,
                "Manual join workflow rejected team={TeamId} channel={ChannelId} code={ErrorCode}",
                teamId, channelId, ex.ErrorCode);
            return BadRequest(new { error = ex.Message, error_code = ex.ErrorCode });
        }
        catch (Exception ex)
        {
            _logger.LogError(ex,
                "Manual join failed team={TeamId} channel={ChannelId}", teamId, channelId);
            return StatusCode(StatusCodes.Status500InternalServerError,
                new { error = ex.Message });
        }
    }
}

public sealed record ReplaceConsumersRequest
{
    [JsonProperty("consumers")] public List<ConsumerConfig>? Consumers { get; init; }
}

public sealed record SetAutoJoinRequest
{
    [JsonProperty("enabled")] public bool? Enabled { get; init; }
}

public sealed record AttachChannelRequest
{
    [JsonProperty("team_id")] public string? TeamId { get; init; }
    [JsonProperty("channel_id")] public string? ChannelId { get; init; }
    [JsonProperty("conversation_thread_id")] public string? ConversationThreadId { get; init; }
    [JsonProperty("team_display_name")] public string? TeamDisplayName { get; init; }
    [JsonProperty("channel_display_name")] public string? ChannelDisplayName { get; init; }
    [JsonProperty("service_url")] public string? ServiceUrl { get; init; }
    [JsonProperty("tenant_id")] public string? TenantId { get; init; }
    [JsonProperty("source")] public string? Source { get; init; }
}

public sealed record ChannelAttachmentResponse
{
    [JsonProperty("team_id")] public required string TeamId { get; init; }
    [JsonProperty("channel_id")] public required string ChannelId { get; init; }
    [JsonProperty("conversation_thread_id")] public string? ConversationThreadId { get; init; }
    [JsonProperty("team_display_name")] public string? TeamDisplayName { get; init; }
    [JsonProperty("channel_display_name")] public string? ChannelDisplayName { get; init; }
    [JsonProperty("source")] public string? Source { get; init; }
    [JsonProperty("attached_at_utc")] public DateTimeOffset AttachedAtUtc { get; init; }
    [JsonProperty("subscription_id")] public string? SubscriptionId { get; init; }
    [JsonProperty("subscription_expires_at_utc")] public DateTimeOffset? SubscriptionExpiresAtUtc { get; init; }
}
