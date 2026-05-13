using Microsoft.AspNetCore.Mvc;
using Microsoft.Bot.Builder;
using Microsoft.Bot.Builder.Integration.AspNet.Core;
using Microsoft.Bot.Schema;
using Newtonsoft.Json;
using System.Collections.Concurrent;
using TeamsMediaBot.Models;
using TeamsMediaBot.Services;

namespace TeamsMediaBot.Controllers;

/// <summary>
/// Internal endpoint the Python sink calls when the Alfred agent decides to
/// post through the send_to_meeting_chat tool. Looks up the
/// ConversationReference captured for the target chat thread and posts via
/// CloudAdapter.ContinueConversationAsync.
///
/// The sink's teams_chat route already rate-limits client-side; we still
/// add a server-side per-chat semaphore as belt-and-suspenders to stay
/// below the Teams 8 RPS soft cap.
/// </summary>
[ApiController]
[Route("api/send-chat")]
public sealed class SendChatController : ControllerBase
{
    private static readonly ConcurrentDictionary<string, SemaphoreSlim> Gates = new();
    private static readonly ConcurrentDictionary<string, DateTimeOffset> RecentRequests = new();
    private static readonly TimeSpan DuplicateWindow = TimeSpan.FromSeconds(20);

    private readonly IBotFrameworkHttpAdapter _adapter;
    private readonly IConversationReferenceStore _references;
    private readonly BotConfiguration _botConfig;
    private readonly EventFanoutDispatcher _dispatcher;
    private readonly ChannelAttachmentStore _attachmentStore;
    private readonly ILogger<SendChatController> _logger;

    public SendChatController(
        IBotFrameworkHttpAdapter adapter,
        IConversationReferenceStore references,
        BotConfiguration botConfig,
        EventFanoutDispatcher dispatcher,
        ChannelAttachmentStore attachmentStore,
        ILogger<SendChatController> logger)
    {
        _adapter = adapter;
        _references = references;
        _botConfig = botConfig;
        _dispatcher = dispatcher;
        _attachmentStore = attachmentStore;
        _logger = logger;
    }

    [HttpPost]
    public async Task<IActionResult> PostAsync([FromBody] SendChatRequest request, CancellationToken ct)
    {
        if (string.IsNullOrWhiteSpace(request.ConversationReferenceId))
        {
            return BadRequest(new { error = "conversation_reference_id is required" });
        }
        if (string.IsNullOrWhiteSpace(request.Text))
        {
            return BadRequest(new { error = "text is required" });
        }

        var conversationReferenceId = request.ConversationReferenceId.Trim();
        var messageText = request.Text.Trim();

        var reference = _references.Get(conversationReferenceId);
        if (reference is null)
        {
            _logger.LogWarning(
                "No ConversationReference cached for {RefId}. Has the bot seen any chat in that thread?",
                conversationReferenceId);
            return NotFound(new { error = "No ConversationReference for that chat; bot must see chat activity first." });
        }

        var dedupeKey = string.Join("|",
            conversationReferenceId,
            request.ReplyToMessageId?.Trim(),
            request.Action?.Trim(),
            messageText);

        var now = DateTimeOffset.UtcNow;
        if (RecentRequests.TryGetValue(dedupeKey, out var lastSeen)
            && now - lastSeen <= DuplicateWindow)
        {
            _logger.LogInformation("Suppressing duplicate send-chat request for {RefId}", conversationReferenceId);
            return Ok(new { ok = true, deduped = true });
        }

        RecentRequests[dedupeKey] = now;

        var gate = Gates.GetOrAdd(conversationReferenceId, static _ => new SemaphoreSlim(1, 1));
        await gate.WaitAsync(ct);
        string? sentActivityId = null;
        try
        {
            var adapter = (BotAdapter)_adapter;
            await adapter.ContinueConversationAsync(
                _botConfig.AppId,
                reference,
                async (turnCtx, innerCt) =>
                {
                    var activity = MessageFactory.Text(messageText);
                    if (!string.IsNullOrWhiteSpace(request.ReplyToMessageId))
                    {
                        activity.ReplyToId = request.ReplyToMessageId;
                    }
                    var response = await turnCtx.SendActivityAsync(activity, innerCt);
                    sentActivityId = response?.Id;
                },
                ct);
        }
        finally
        {
            gate.Release();
        }

        // Mirror the outbound reply into the same chat.message stream we
        // publish for inbound activities. Without this, archive +
        // consumer fan-out see only the human side of the conversation,
        // not Alfred's replies. Team/channel ids are looked up from the
        // channel attachment store so the blob lands at
        // channels/{teamId}/{channelId}/chat.message/... when the chat
        // is a channel post (matches the inbound path's blob layout).
        await PublishOutboundChatAsync(conversationReferenceId, messageText, sentActivityId, request.ReplyToMessageId, ct);

        return Ok(new { ok = true });
    }

    /// <summary>
    /// Emits an envelope describing the bot's outbound reply. Best-effort —
    /// archive failures must never bubble up and 500 the API call the
    /// Python sink is awaiting. The dispatcher's own paths swallow per-
    /// consumer errors internally; only synchronous publish exceptions
    /// would surface here, which would be code bugs worth logging loudly.
    /// </summary>
    private async Task PublishOutboundChatAsync(
        string conversationReferenceId,
        string messageText,
        string? sentActivityId,
        string? replyToMessageId,
        CancellationToken cancellationToken)
    {
        try
        {
            // For channel posts, the conversation reference id IS the
            // channel thread id (19:{channelId}@thread.tacv2). The
            // attachment store is keyed by (teamId, channelId) so we walk
            // its records to find the one whose conversation_thread_id
            // matches, giving us team/channel ids for the envelope.
            var attachment = _attachmentStore.GetByConversationThreadId(conversationReferenceId);

            var payload = new ChatEventPayload
            {
                EventType = "chat_created",
                ChatThreadId = conversationReferenceId,
                MessageId = sentActivityId ?? Guid.NewGuid().ToString("N"),
                Text = messageText,
                SenderId = _botConfig.AppId,
                SenderDisplayName = "Alfred",
                TimestampUtc = DateTimeOffset.UtcNow.ToString("o"),
                ConversationReferenceId = conversationReferenceId,
                ReplyToMessageId = replyToMessageId,
                FromBot = true,
                ConversationKind = attachment is not null ? "channel" : "meeting_chat",
                TeamId = attachment?.TeamId,
                ChannelId = attachment?.ChannelId,
                ChannelThreadId = attachment?.ChannelId,
            };

            await _dispatcher.PublishAsync(
                new AlfredEventEnvelope
                {
                    EventType = AlfredEventTypes.ChatMessage,
                    EventId = Guid.NewGuid().ToString("N"),
                    Ts = payload.TimestampUtc,
                    TeamId = payload.TeamId,
                    ChannelId = payload.ChannelId,
                    ChatThreadId = conversationReferenceId,
                    ChannelThreadId = payload.ChannelThreadId,
                    ConversationReferenceId = conversationReferenceId,
                    Payload = payload,
                },
                cancellationToken);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex,
                "Failed to publish outbound chat envelope for ConversationReferenceId={RefId}",
                conversationReferenceId);
        }
    }
}

public sealed record SendChatRequest
{
    [JsonProperty("conversation_reference_id")]
    public string? ConversationReferenceId { get; init; }

    [JsonProperty("action")]
    public string? Action { get; init; }

    [JsonProperty("text")]
    public string? Text { get; init; }

    [JsonProperty("mentions")]
    public List<string>? Mentions { get; init; }

    [JsonProperty("reply_to_message_id")]
    public string? ReplyToMessageId { get; init; }

    [JsonProperty("rationale")]
    public string? Rationale { get; init; }

    [JsonProperty("session_id")]
    public string? SessionId { get; init; }

    [JsonProperty("product_id")]
    public string? ProductId { get; init; }

    [JsonProperty("instance_id")]
    public string? InstanceId { get; init; }
}
