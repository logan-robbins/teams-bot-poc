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
    private readonly EventFanoutDispatcher _dispatcher;
    private readonly IChannelAttachmentService _channelAttachments;
    private readonly TeamsCallingBotService _botService;
    private readonly TranscriberFactory _transcriberFactory;
    private readonly BotConfiguration _botConfig;
    private readonly GraphApiClient _graph;
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, byte> _publishedLinks =
        new(StringComparer.Ordinal);
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, DateTimeOffset> _meetingJoinAttempts =
        new(StringComparer.Ordinal);
    private readonly ILogger<AlfredBot> _logger;

    public AlfredBot(
        IConversationReferenceStore references,
        EventFanoutDispatcher dispatcher,
        IChannelAttachmentService channelAttachments,
        TeamsCallingBotService botService,
        TranscriberFactory transcriberFactory,
        BotConfiguration botConfig,
        GraphApiClient graph,
        ILogger<AlfredBot> logger)
    {
        _references = references;
        _dispatcher = dispatcher;
        _channelAttachments = channelAttachments;
        _botService = botService;
        _transcriberFactory = transcriberFactory;
        _botConfig = botConfig;
        _graph = graph;
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
        else if (botAdded)
        {
            // Bot was added to a non-team context (meeting chat or
            // group chat). For meeting chats specifically, "added to
            // chat" is the user signalling intent for Alfred to join
            // the call. Fire the auto-join.
            var chatThreadId = activity.Conversation?.Id;
            if (LooksLikeMeetingChat(chatThreadId))
            {
                _ = Task.Run(() => TryAutoJoinMeetingChatAsync(chatThreadId!, "added_to_meeting_chat"));
            }
        }

        await base.OnMembersAddedAsync(membersAdded, turnContext, cancellationToken);
    }

    /// <summary>
    /// Heuristic — recognizes a Teams thread id that is bound to an
    /// active meeting:
    ///   • <c>19:meeting_xxx@thread.v2</c>           — scheduled / ad-hoc meeting
    ///   • <c>19:*@thread.tacv2;messageid=xxx</c>    — channel meeting (the
    ///     messageid suffix is Teams' way of pinning the activity to the
    ///     channel-meeting announcement)
    /// </summary>
    private static bool LooksLikeMeetingChat(string? chatThreadId)
    {
        if (string.IsNullOrWhiteSpace(chatThreadId)) return false;
        if (chatThreadId.Contains("meeting_", StringComparison.OrdinalIgnoreCase)
            && chatThreadId.Contains("@thread.v2", StringComparison.OrdinalIgnoreCase))
        {
            return true;
        }
        if (chatThreadId.Contains("@thread.tacv2", StringComparison.OrdinalIgnoreCase)
            && chatThreadId.Contains(";messageid=", StringComparison.OrdinalIgnoreCase))
        {
            return true;
        }
        return false;
    }

    /// <summary>
    /// Auto-join a meeting Alfred was just added to (or @-mentioned in).
    /// Synthesizes the meeting join URL from the chat thread id and the
    /// bot's tenant; the SDK uses it to bind to the real meeting and the
    /// bot's chat-scoped RSC carries the join permission. Per-thread
    /// dedupe with a 60s window so we don't double-join on rapid retries
    /// (multiple membersAdded + @-mention firing back-to-back).
    /// </summary>
    private async Task TryAutoJoinMeetingChatAsync(string chatThreadId, string source)
    {
        var now = DateTimeOffset.UtcNow;
        if (_meetingJoinAttempts.TryGetValue(chatThreadId, out var prev)
            && now - prev < TimeSpan.FromSeconds(60))
        {
            _logger.LogDebug(
                "Skipping auto-join for thread={Thread} source={Source}; previous attempt {Age}s ago.",
                chatThreadId, source, (int)(now - prev).TotalSeconds);
            return;
        }
        _meetingJoinAttempts[chatThreadId] = now;

        try
        {
            var tenantId = _botConfig.TenantId;
            if (string.IsNullOrWhiteSpace(tenantId))
            {
                _logger.LogWarning("Auto-join skipped: BotConfig.TenantId is unset.");
                return;
            }

            var joinUrl = ChannelMeetingJoinUrls.Build(
                chatThreadId,
                tenantId!,
                _botConfig.AppId ?? string.Empty);

            _logger.LogInformation(
                "Auto-joining meeting chat thread={Thread} source={Source}",
                chatThreadId, source);

            var transcriber = _transcriberFactory.Create();
            var result = await _botService.JoinMeetingWithModeAsync(
                new JoinMeetingCommand
                {
                    JoinUrl = joinUrl,
                    DisplayName = "Alfred",
                    JoinAsGuest = false,
                    RequestedJoinMode = JoinModeNames.InviteAndGraphJoin,
                    OrganizerTenantId = tenantId,
                    // The bot is installed in the meeting chat (that's
                    // the only way these activities reached us), so the
                    // chat-scoped Calls.JoinGroupCalls.Chat RSC covers
                    // the join. Bot attendee check is moot.
                    BotAttendeePresent = true,
                },
                transcriber).ConfigureAwait(false);

            _logger.LogInformation(
                "Meeting auto-join result thread={Thread} CallId={CallId} Mode={Mode} Deferred={Deferred} Msg={Msg}",
                chatThreadId, result.CallId, result.SelectedJoinMode, result.Deferred, result.Message);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex,
                "Meeting auto-join failed thread={Thread} source={Source}",
                chatThreadId, source);
            // Drop the dedupe so a later @-mention can retry.
            _meetingJoinAttempts.TryRemove(chatThreadId, out _);
        }
    }

    /// <summary>
    /// Idempotent attach: creates the channel attachment record if it
    /// doesn't exist; otherwise just enriches missing display names so
    /// the admin UI reads "Engineering / General" instead of the GUIDs.
    /// </summary>
    private async Task EnsureChannelAttachedAsync(
        ITurnContext turnContext,
        string teamId,
        string channelId,
        TeamsChannelData? channelData,
        CancellationToken cancellationToken)
    {
        try
        {
            var existing = _channelAttachments.Get(teamId, channelId);
            var existingTeamName = existing?.TeamDisplayName;
            var existingChannelName = existing?.ChannelDisplayName;
            var needsAttach = existing is null;
            var needsNameEnrichment =
                string.IsNullOrWhiteSpace(existingTeamName) ||
                string.IsNullOrWhiteSpace(existingChannelName);

            if (!needsAttach && !needsNameEnrichment) return;

            // Try the activity's TeamsChannelData first (cheap, no
            // network). For channel chats Teams often omits these.
            var teamName = channelData?.Team?.Name ?? existingTeamName;
            var channelName = channelData?.Channel?.Name ?? existingChannelName;

            // Fill the rest via Microsoft Graph using the bot's
            // app-scoped token. RSC permissions TeamSettings.Read.Group
            // and ChannelSettings.Read.Group are granted at install
            // time, so no org-wide consent is required.
            //
            // We tried Bot Framework's TeamsInfo helpers first, but the
            // Calling-bot connector's audience makes them return 400
            // BadRequest for FetchTeamDetailsAsync /
            // FetchChannelListAsync. Graph is the working path.
            if (string.IsNullOrWhiteSpace(teamName))
            {
                try
                {
                    using var doc = await _graph.GetResourceAsync(
                        $"teams/{Uri.EscapeDataString(teamId)}",
                        cancellationToken);
                    if (doc.RootElement.TryGetProperty("displayName", out var dn) &&
                        dn.ValueKind == System.Text.Json.JsonValueKind.String)
                    {
                        teamName = dn.GetString();
                    }
                }
                catch (Exception ex)
                {
                    _logger.LogWarning(ex,
                        "Graph GET /teams/{TeamId} failed", teamId);
                }
            }
            if (string.IsNullOrWhiteSpace(channelName))
            {
                try
                {
                    using var doc = await _graph.GetResourceAsync(
                        $"teams/{Uri.EscapeDataString(teamId)}/channels/{Uri.EscapeDataString(channelId)}",
                        cancellationToken);
                    if (doc.RootElement.TryGetProperty("displayName", out var dn) &&
                        dn.ValueKind == System.Text.Json.JsonValueKind.String)
                    {
                        channelName = dn.GetString();
                    }
                }
                catch (Exception ex)
                {
                    _logger.LogWarning(ex,
                        "Graph GET /teams/{TeamId}/channels/{ChannelId} failed",
                        teamId, channelId);
                }
            }

            // Upsert with whatever names we resolved. Existing consumer
            // lists / auto-join state / subscription are preserved by
            // AttachAsync's merge-with-existing semantics.
            await _channelAttachments.AttachAsync(
                new ChannelAttachmentRequest
                {
                    TeamId = teamId,
                    ChannelId = channelId,
                    ConversationThreadId = channelId,
                    ChannelDisplayName = channelName,
                    TeamDisplayName = teamName,
                    ServiceUrl = turnContext.Activity.ServiceUrl,
                    TenantId = channelData?.Tenant?.Id,
                    Source = existing?.Source ?? "auto_attach_on_chat",
                },
                cancellationToken);

            if (needsAttach)
            {
                _logger.LogInformation(
                    "Auto-attached channel on first chat TeamName='{Team}' ChannelName='{Channel}' TeamId={TeamId} ChannelId={ChannelId}",
                    teamName, channelName, teamId, channelId);
            }
            else
            {
                _logger.LogInformation(
                    "Enriched display names on existing attachment TeamName='{Team}' ChannelName='{Channel}' TeamId={TeamId} ChannelId={ChannelId}",
                    teamName, channelName, teamId, channelId);
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex,
                "EnsureChannelAttachedAsync failed TeamId={TeamId} ChannelId={ChannelId}",
                teamId, channelId);
        }
    }

    /// <summary>True when an activity contains an @-mention of Alfred.</summary>
    private bool WasBotMentioned(IMessageActivity activity)
    {
        var mentions = activity.GetMentions();
        if (mentions is null || mentions.Length == 0) return false;
        var botId = _botConfig.AppId;
        foreach (var m in mentions)
        {
            var mentionedId = m.Mentioned?.Id ?? string.Empty;
            // Bot Framework mention ids look like "28:<appid>" — match by
            // the suffix to be safe across BF id formats.
            if (!string.IsNullOrWhiteSpace(botId) && mentionedId.EndsWith(botId, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
            // Belt-and-suspenders: also match by the bot's name from the
            // recipient field on the activity.
            var recipientId = activity.Recipient?.Id ?? string.Empty;
            if (!string.IsNullOrWhiteSpace(recipientId) && string.Equals(mentionedId, recipientId, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }
        return false;
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

        await _dispatcher.PublishAsync(
            new AlfredEventEnvelope
            {
                EventType = AlfredEventTypes.ChatMessage,
                EventId = Guid.NewGuid().ToString("N"),
                Ts = payload.TimestampUtc,
                TeamId = teamId,
                ChannelId = channelId,
                ChatThreadId = chatThreadId,
                ChannelThreadId = channelId,
                ConversationReferenceId = chatThreadId,
                Payload = payload,
            },
            cancellationToken);

        // Bot Framework delivers channel chat activities even when
        // OnMembersAddedAsync didn't fire (Teams doesn't always fire
        // it for channel installs). When we see a channel chat with
        // full team+channel context, ensure we have an attachment with
        // friendly display names so the admin UI shows "Engineering /
        // General" instead of GUIDs. Idempotent on existing names.
        if (!string.IsNullOrWhiteSpace(teamId)
            && !string.IsNullOrWhiteSpace(channelId))
        {
            // We need the turnContext for TeamsInfo lookups, so do this
            // inline (await) — TeamsInfo round-trips through Bot
            // Framework, takes ~100ms.
            await EnsureChannelAttachedAsync(
                turnContext, teamId!, channelId!, channelData, cancellationToken);
        }

        // If Alfred was @-mentioned in any meeting (regular or channel
        // meeting) AND isn't already in the call, treat it as user
        // intent to bring him in. Per-thread dedupe inside
        // TryAutoJoinMeetingChatAsync handles repeated mentions.
        if (LooksLikeMeetingChat(chatThreadId) && WasBotMentioned(activity))
        {
            _ = Task.Run(() => TryAutoJoinMeetingChatAsync(chatThreadId, "at_mention"));
        }

        // If this activity is in a meeting chat that was spawned from a
        // channel (channelData carries team + channel, but the chat
        // thread id is the meeting's thread, not the channel's),
        // emit a session-linked event so consumers can roll the meeting
        // under its parent channel. De-duped per (chat_thread_id, team,
        // channel) so we don't emit one on every chat activity.
        if (!string.IsNullOrWhiteSpace(teamId)
            && !string.IsNullOrWhiteSpace(channelId)
            && !string.Equals(chatThreadId, channelId, StringComparison.Ordinal))
        {
            var linkKey = string.Join("|", chatThreadId, teamId, channelId);
            if (_publishedLinks.TryAdd(linkKey, 1))
            {
                await _dispatcher.PublishAsync(
                    new AlfredEventEnvelope
                    {
                        EventType = AlfredEventTypes.SessionLinked,
                        EventId = Guid.NewGuid().ToString("N"),
                        Ts = DateTimeOffset.UtcNow.ToString("O"),
                        TeamId = teamId,
                        ChannelId = channelId,
                        ChatThreadId = chatThreadId,
                        ChannelThreadId = channelId,
                        ConversationReferenceId = chatThreadId,
                        Payload = new SessionLinkedPayload
                        {
                            ChatThreadId = chatThreadId,
                            TeamId = teamId!,
                            ChannelId = channelId!,
                            ChannelThreadId = channelId,
                            Source = "bot_framework_channeldata",
                        },
                    },
                    cancellationToken);
            }
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
