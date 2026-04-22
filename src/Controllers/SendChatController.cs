using Microsoft.AspNetCore.Mvc;
using Microsoft.Bot.Builder;
using Microsoft.Bot.Builder.Integration.AspNet.Core;
using Microsoft.Bot.Schema;
using System.Collections.Concurrent;
using TeamsMediaBot.Models;
using TeamsMediaBot.Services;

namespace TeamsMediaBot.Controllers;

/// <summary>
/// Internal endpoint the Python sink calls when the Alfred agent decides to
/// SEND or ASK. Looks up the ConversationReference captured for the target
/// chat thread and posts via CloudAdapter.ContinueConversationAsync.
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
    private readonly MeetingChatConfiguration _chatConfig;
    private readonly ILogger<SendChatController> _logger;

    public SendChatController(
        IBotFrameworkHttpAdapter adapter,
        IConversationReferenceStore references,
        BotConfiguration botConfig,
        MeetingChatConfiguration chatConfig,
        ILogger<SendChatController> logger)
    {
        _adapter = adapter;
        _references = references;
        _botConfig = botConfig;
        _chatConfig = chatConfig;
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
                    await turnCtx.SendActivityAsync(activity, innerCt);
                },
                ct);
        }
        finally
        {
            gate.Release();
        }

        return Ok(new { ok = true });
    }
}

public sealed record SendChatRequest
{
    public string? ConversationReferenceId { get; init; }
    public string? Action { get; init; }
    public string? Text { get; init; }
    public List<string>? Mentions { get; init; }
    public string? ReplyToMessageId { get; init; }
    public string? Rationale { get; init; }
    public string? SessionId { get; init; }
    public string? ProductId { get; init; }
    public string? InstanceId { get; init; }
}
