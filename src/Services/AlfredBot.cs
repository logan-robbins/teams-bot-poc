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
///   - Capture a ConversationReference for every conversation the bot is
///     installed in (meeting chats, group chats, team channels). This is
///     required for proactive sends via CloudAdapter.ContinueConversationAsync.
///   - Forward every inbound chat/channel message to the Python sink's /chat
///     endpoint so the unified meeting timeline stays complete.
///   - Stamp <c>conversation_kind</c>, <c>team_id</c>, <c>channel_id</c> on
///     the chat payload so the sink can key channel sessions correctly.
///   - Auto-attach to a team's General channel when the bot is added to a
///     team, so a single Teams-side "install" turns into persistent channel
///     listening with no per-meeting setup.
///
/// The bot does NOT respond inline to chat messages; all outbound speech is
/// driven by the Python sink via SendChatController.
/// </summary>
public sealed class AlfredBot : TeamsActivityHandler
{
    private readonly IConversationReferenceStore _references;
    private readonly PythonChatPublisher _chatPublisher;
    private readonly IChannelAttachmentService _channelAttachments;
    private readonly ChannelLinkPublisher _channelLinkPublisher;
    private readonly ILogger<AlfredBot> _logger;

    public AlfredBot(
        IConversationReferenceStore references,
        PythonChatPublisher chatPublisher,
        IChannelAttachmentService channelAttachments,
        ChannelLinkPublisher channelLinkPublisher,
        ILogger<AlfredBot> logger)
    {
        _references = references;
        _chatPublisher = chatPublisher;
        _channelAttachments = channelAttachments;
        _channelLinkPublisher = channelLinkPublisher;
        _logger = logger;
    }

    protected override async Task OnConversationUpdateActivityAsync(
        ITurnContext<IConversationUpdateActivity> turnContext,
        CancellationToken cancellationToken)
    {
        CaptureConversationReference(turnContext);
        await base.OnConversationUpdateActivityAsync(turnContext, cancellationToken);
    }

    protected override async Task OnMembersAddedAsync(
        IList<ChannelAccount> membersAdded,
        ITurnContext<IConversationUpdateActivity> turnContext,
        CancellationToken cancellationToken)
    {
        var activity = turnContext.Activity;
        var channelData = TryGetChannelData(activity);
        var teamId = ResolveTeamId(channelData);
        var channelId = channelData?.Channel?.Id;
        var botAdded = membersAdded.Any(m => string.Equals(
            m.Id,
            activity.Recipient?.Id,
            StringComparison.Ordinal));

        if (botAdded
            && !string.IsNullOrWhiteSpace(teamId)
            && !string.IsNullOrWhiteSpace(channelId))
        {
            try
            {
                await _channelAttachments.AttachAsync(
                    new ChannelAttachmentRequest
                    {
                        TeamId = teamId,
                        ChannelId = channelId,
                        ConversationThreadId = activity.Conversation?.Id,
                        ChannelDisplayName = channelData?.Channel?.Name,
                        TeamDisplayName = channelData?.Team?.Name,
                        ServiceUrl = activity.ServiceUrl,
                        TenantId = channelData?.Tenant?.Id,
                        Source = "team_install",
                    },
                    cancellationToken);

                _logger.LogInformation(
                    "Auto-attached Alfred to channel TeamId={TeamId} ChannelId={ChannelId} via team install",
                    teamId,
                    channelId);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(
                    ex,
                    "Failed to auto-attach to channel TeamId={TeamId} ChannelId={ChannelId} on team install",
                    teamId,
                    channelId);
            }
        }

        await base.OnMembersAddedAsync(membersAdded, turnContext, cancellationToken);
    }

    /// <summary>
    /// Returns the team's AAD group id, which is what Microsoft Graph URLs
    /// require as <c>{team-id}</c>. Bot Framework's <c>TeamInfo.Id</c> usually
    /// matches the AAD group id but isn't guaranteed to (legacy/migrated
    /// teams can differ), so prefer <c>AadGroupId</c> when populated.
    /// </summary>
    private static string? ResolveTeamId(TeamsChannelData? channelData) =>
        channelData?.Team?.AadGroupId ?? channelData?.Team?.Id;

    protected override async Task OnMessageActivityAsync(
        ITurnContext<IMessageActivity> turnContext,
        CancellationToken cancellationToken)
    {
        CaptureConversationReference(turnContext);

        var activity = turnContext.Activity;
        var chatThreadId = ExtractChatThreadId(activity);
        if (string.IsNullOrWhiteSpace(chatThreadId))
        {
            return;
        }

        var channelData = TryGetChannelData(activity);
        var conversationKind = ResolveConversationKind(activity, channelData);
        var teamId = ResolveTeamId(channelData);
        var channelId = channelData?.Channel?.Id;

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
            ConversationKind = conversationKind,
            TeamId = teamId,
            ChannelId = channelId,
            ChannelThreadId = channelId,
        };

        await _chatPublisher.PublishAsync(payload, cancellationToken);

        // If this activity is in a meeting chat that was spawned from a
        // channel (channelData carries team + channel, but the chat
        // thread id is the meeting's thread, not the channel's),
        // tell the sink so every transcript / chat / system event for
        // this meeting can later be rolled up under the parent channel.
        if (!string.IsNullOrWhiteSpace(teamId)
            && !string.IsNullOrWhiteSpace(channelId)
            && !string.Equals(chatThreadId, channelId, StringComparison.Ordinal))
        {
            _ = _channelLinkPublisher.PublishLinkAsync(
                chatThreadId,
                teamId!,
                channelId!,
                channelId,
                source: "bot_framework_channeldata",
                cancellationToken: cancellationToken);
        }
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
        _logger.LogInformation(
            "Captured ConversationReference for thread {ChatThreadId} (kind={Kind})",
            chatThreadId,
            ResolveConversationKind(turnContext.Activity, TryGetChannelData(turnContext.Activity)));
    }

    private static string? ExtractChatThreadId(IActivity activity)
    {
        var convId = activity?.Conversation?.Id;
        return string.IsNullOrWhiteSpace(convId) ? null : convId;
    }

    private static TeamsChannelData? TryGetChannelData(IActivity activity)
    {
        if (activity is null)
        {
            return null;
        }

        try
        {
            return activity.GetChannelData<TeamsChannelData>();
        }
        catch
        {
            return null;
        }
    }

    private static string ResolveConversationKind(IActivity activity, TeamsChannelData? channelData)
    {
        var raw = (activity?.Conversation?.ConversationType ?? string.Empty).ToLowerInvariant();
        if (channelData?.Team is not null
            && (string.Equals(raw, "channel", StringComparison.Ordinal)
                || !string.IsNullOrWhiteSpace(channelData.Channel?.Id)))
        {
            return "channel";
        }

        var convId = activity?.Conversation?.Id ?? string.Empty;
        if (convId.Contains("@thread.v2", StringComparison.OrdinalIgnoreCase)
            || convId.Contains("meeting_", StringComparison.OrdinalIgnoreCase))
        {
            return "meeting_chat";
        }

        return string.IsNullOrWhiteSpace(raw) ? "unknown" : raw;
    }
}
