using Microsoft.AspNetCore.Mvc;
using Newtonsoft.Json;
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
    private readonly ILogger<ChannelAttachmentController> _logger;

    public ChannelAttachmentController(
        IChannelAttachmentService service,
        ILogger<ChannelAttachmentController> logger)
    {
        _service = service;
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
}

public sealed record ReplaceConsumersRequest
{
    [JsonProperty("consumers")] public List<ConsumerConfig>? Consumers { get; init; }
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
