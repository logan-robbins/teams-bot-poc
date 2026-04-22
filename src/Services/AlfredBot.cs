using Microsoft.Bot.Builder;
using Microsoft.Bot.Builder.Teams;
using Microsoft.Bot.Schema;
using Microsoft.Bot.Schema.Teams;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Bot Framework activity handler for Alfred.
///
/// Responsibilities:
///   - Capture a ConversationReference for every meeting chat the bot is
///     installed in. This is required for proactive sends via
///     CloudAdapter.ContinueConversationAsync.
///   - Forward every inbound chat message to the Python sink's /chat
///     endpoint so the unified meeting timeline stays complete even when
///     the Graph change-notification path is disabled or still rolling out.
///
/// The bot does NOT respond inline to chat messages; all outbound speech is
/// driven by the Python sink via SendChatController.
/// </summary>
public sealed class AlfredBot : TeamsActivityHandler
{
    private readonly IConversationReferenceStore _references;
    private readonly PythonChatPublisher _chatPublisher;
    private readonly ILogger<AlfredBot> _logger;

    public AlfredBot(
        IConversationReferenceStore references,
        PythonChatPublisher chatPublisher,
        ILogger<AlfredBot> logger)
    {
        _references = references;
        _chatPublisher = chatPublisher;
        _logger = logger;
    }

    protected override Task OnConversationUpdateActivityAsync(
        ITurnContext<IConversationUpdateActivity> turnContext,
        CancellationToken cancellationToken)
    {
        CaptureConversationReference(turnContext);
        return base.OnConversationUpdateActivityAsync(turnContext, cancellationToken);
    }

    protected override async Task OnMessageActivityAsync(
        ITurnContext<IMessageActivity> turnContext,
        CancellationToken cancellationToken)
    {
        CaptureConversationReference(turnContext);

        var activity = turnContext.Activity;
        var chatThreadId = ExtractChatThreadId(activity);
        if (string.IsNullOrWhiteSpace(chatThreadId))
        {
            return; // not a meeting chat
        }

        var payload = new ChatEventPayload
        {
            EventType = "chat_created",
            ChatThreadId = chatThreadId,
            MessageId = activity.Id ?? Guid.NewGuid().ToString("N"),
            Text = activity.Text,
            Html = activity.Attachments?.FirstOrDefault(a => a.ContentType == "text/html")?.Content?.ToString(),
            SenderId = activity.From?.AadObjectId ?? activity.From?.Id,
            SenderDisplayName = activity.From?.Name,
            TimestampUtc = (activity.Timestamp ?? DateTimeOffset.UtcNow).UtcDateTime.ToString("o"),
            ConversationReferenceId = chatThreadId,
            ReplyToMessageId = activity.ReplyToId,
            FromBot = activity.From?.Role == "bot",
        };

        await _chatPublisher.PublishAsync(payload, cancellationToken);
    }

    private void CaptureConversationReference(ITurnContext turnContext)
    {
        var chatThreadId = ExtractChatThreadId(turnContext.Activity);
        if (string.IsNullOrWhiteSpace(chatThreadId))
        {
            return;
        }
        var reference = turnContext.Activity.GetConversationReference();
        _references.Put(chatThreadId, reference);
        _logger.LogInformation("Captured ConversationReference for chat thread {ChatThreadId}", chatThreadId);
    }

    private static string? ExtractChatThreadId(IActivity activity)
    {
        // Teams meeting chats have conversation.id starting with "19:" and
        // containing "meeting_" or "@thread.v2". We treat the raw id as the
        // chat thread id (Graph calls this chatInfo.threadId).
        var convId = activity?.Conversation?.Id;
        return string.IsNullOrWhiteSpace(convId) ? null : convId;
    }
}
