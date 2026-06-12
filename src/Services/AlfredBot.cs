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
    private readonly ChannelAttachmentStore _attachmentStore;
    private readonly MeetingChannelLinkStore _meetingLinks;
    private readonly TeamsCallingBotService _botService;
    private readonly TranscriberFactory _transcriberFactory;
    private readonly BotConfiguration _botConfig;
    private readonly GraphApiClient _graph;
    private readonly GraphMetadataResolver _metadataResolver;
    private readonly OfficialTranscriptFetcher _transcriptFetcher;
    private readonly ClientRouteResolver _clientRoutes;
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, byte> _publishedMeetingCreated =
        new(StringComparer.Ordinal);
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, byte> _publishedLinks =
        new(StringComparer.Ordinal);
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, DateTimeOffset> _meetingJoinAttempts =
        new(StringComparer.Ordinal);
    private readonly ILogger<AlfredBot> _logger;

    public AlfredBot(
        IConversationReferenceStore references,
        EventFanoutDispatcher dispatcher,
        IChannelAttachmentService channelAttachments,
        ChannelAttachmentStore attachmentStore,
        MeetingChannelLinkStore meetingLinks,
        TeamsCallingBotService botService,
        TranscriberFactory transcriberFactory,
        BotConfiguration botConfig,
        GraphApiClient graph,
        GraphMetadataResolver metadataResolver,
        OfficialTranscriptFetcher transcriptFetcher,
        ClientRouteResolver clientRoutes,
        ILogger<AlfredBot> logger)
    {
        _references = references;
        _dispatcher = dispatcher;
        _channelAttachments = channelAttachments;
        _attachmentStore = attachmentStore;
        _meetingLinks = meetingLinks;
        _botService = botService;
        _transcriberFactory = transcriberFactory;
        _botConfig = botConfig;
        _graph = graph;
        _metadataResolver = metadataResolver;
        _transcriptFetcher = transcriptFetcher;
        _clientRoutes = clientRoutes;
        _logger = logger;
    }

    /// <summary>
    /// Email-based client routing (PLAN.md): resolve the best candidate
    /// people for this meeting and bind the chat thread to a registered
    /// client route. Cheap no-op when no enabled routes exist or the
    /// thread is already bound. TeamsInfo supplies emails through Bot
    /// Framework, which works with RSC-only grants; the resolver falls
    /// back to the alias table and Graph. Never throws.
    /// </summary>
    private async Task TryBindClientRouteAsync(
        ITurnContext turnContext,
        string chatThreadId,
        string? meetingId,
        IReadOnlyList<ClientIdentityCandidate> candidates,
        CancellationToken cancellationToken)
    {
        try
        {
            if (!_clientRoutes.NeedsBinding(chatThreadId)) return;

            var enriched = new List<ClientIdentityCandidate>(candidates.Count);
            foreach (var candidate in candidates)
            {
                if (!string.IsNullOrWhiteSpace(candidate.Email)
                    || string.IsNullOrWhiteSpace(candidate.AadObjectId))
                {
                    enriched.Add(candidate);
                    continue;
                }

                string? email = null;
                try
                {
                    var member = await TeamsInfo.GetMemberAsync(
                        turnContext, candidate.AadObjectId, cancellationToken);
                    email = member?.Email ?? member?.UserPrincipalName;
                }
                catch (Exception ex)
                {
                    _logger.LogDebug(ex,
                        "TeamsInfo.GetMemberAsync failed for Aad={Aad} in thread={Thread}; resolver will fall back.",
                        candidate.AadObjectId, chatThreadId);
                }
                enriched.Add(candidate with { Email = email });
            }

            await _clientRoutes.BindMeetingAsync(chatThreadId, meetingId, enriched, cancellationToken);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex,
                "Client route binding failed for thread={Thread}; events stay on the fallback path.",
                chatThreadId);
        }
    }

    protected override async Task OnConversationUpdateActivityAsync(
        ITurnContext<IConversationUpdateActivity> turnContext,
        CancellationToken cancellationToken)
    {
        CaptureConversationReference(turnContext);
        await base.OnConversationUpdateActivityAsync(turnContext, cancellationToken);
    }

    /// <summary>
    /// Meeting-start event. Fires when a meeting where Alfred is
    /// installed starts. Requires manifest RSC
    /// <c>OnlineMeeting.ReadBasic.Chat</c> (consented at "+Apps" install).
    /// Payload <c>MeetingStartEventDetails</c> carries the meeting subject
    /// (<c>Title</c>), join URL, start time, and meeting id directly —
    /// no TeamsInfo lookup needed for the subject. Per Microsoft docs
    /// (apps-in-teams-meetings/meeting-apps-apis): "Get meeting ID from
    /// turnContext.ChannelData. Do not use meeting ID from meeting
    /// events payload turncontext.activity.value." We follow that.
    /// </summary>
    protected override async Task OnTeamsMeetingStartAsync(
        MeetingStartEventDetails meeting,
        ITurnContext<IEventActivity> turnContext,
        CancellationToken cancellationToken)
    {
        var activity = turnContext.Activity;
        var chatThreadId = activity.Conversation?.Id;
        if (string.IsNullOrWhiteSpace(chatThreadId))
        {
            await base.OnTeamsMeetingStartAsync(meeting, turnContext, cancellationToken);
            return;
        }

        // Canonical Graph meeting id, per docs: read from
        // ChannelData.meeting.id (NOT from activity.value).
        var channelData = TryGetChannelData(activity);
        var canonicalMeetingId = channelData?.Meeting?.Id ?? meeting?.Id?.ToString();
        var subject = meeting?.Title?.Trim();
        var joinUrl = meeting?.JoinUrl?.ToString();
        var startTime = meeting?.StartTime.ToString("O");

        SenderRef? organizer = null;
        try
        {
            var info = await TeamsInfo.GetMeetingInfoAsync(turnContext, cancellationToken: cancellationToken);
            var orgAad = info?.Organizer?.AadObjectId;
            if (!string.IsNullOrWhiteSpace(orgAad))
            {
                organizer = new SenderRef
                {
                    AadId = orgAad,
                    DisplayName = info!.Organizer.Name,
                };
            }
            // Prefer TeamsInfo MsGraphResourceId if available — it's the
            // resolved canonical form Graph's transcripts endpoint wants.
            var infoMid = info?.Details?.MsGraphResourceId;
            if (!string.IsNullOrWhiteSpace(infoMid))
            {
                canonicalMeetingId = infoMid;
            }
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex,
                "TeamsInfo.GetMeetingInfoAsync inside OnTeamsMeetingStartAsync failed (non-fatal) for ChatThreadId={ChatThreadId}",
                chatThreadId);
        }

        _logger.LogInformation(
            "OnTeamsMeetingStartAsync ChatThreadId={ChatThreadId} CanonicalMeetingId={MeetingId} Subject={Subject} Organizer={Organizer} JoinUrl={JoinUrl}",
            chatThreadId,
            canonicalMeetingId ?? "(null)",
            subject ?? "(null)",
            organizer?.DisplayName ?? "(null)",
            joinUrl ?? "(null)");

        if (_publishedMeetingCreated.TryAdd(chatThreadId, 1))
        {
            try
            {
                await _dispatcher.PublishAsync(
                    new AlfredEventEnvelope
                    {
                        EventType = AlfredEventTypes.MeetingCreated,
                        EventId = Guid.NewGuid().ToString("N"),
                        Ts = DateTimeOffset.UtcNow.ToString("O"),
                        MeetingRef = new MeetingRef
                        {
                            MeetingId = canonicalMeetingId ?? chatThreadId,
                            MeetingChatThreadId = chatThreadId,
                            Subject = subject,
                            Organizer = organizer,
                            ScheduledStartUtc = startTime,
                        },
                        ConversationReferenceId = chatThreadId,
                        Payload = new MeetingLifecyclePayload
                        {
                            Subject = subject,
                            Organizer = organizer,
                            ScheduledStartUtc = startTime,
                        },
                    },
                    cancellationToken);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex,
                    "Failed to publish meeting.created from OnTeamsMeetingStartAsync ChatThreadId={ChatThreadId}",
                    chatThreadId);
                _publishedMeetingCreated.TryRemove(chatThreadId, out _);
            }
        }

        if (organizer?.AadId is not null)
        {
            await TryBindClientRouteAsync(
                turnContext,
                chatThreadId,
                canonicalMeetingId,
                new[]
                {
                    new ClientIdentityCandidate
                    {
                        AadObjectId = organizer.AadId,
                        DisplayName = organizer.DisplayName,
                        Source = "organizer",
                    },
                },
                cancellationToken);
        }

        await base.OnTeamsMeetingStartAsync(meeting, turnContext, cancellationToken);
    }

    /// <summary>
    /// Meeting-end event. Fires when a meeting where Alfred is installed
    /// ends. Mirrors Microsoft's meetings-transcription sample at
    /// <c>OfficeDev/Microsoft-Teams-Samples/samples/meetings-transcription/csharp</c>:
    /// use <see cref="TeamsInfo.GetMeetingInfoAsync"/> to grab the
    /// canonical Graph onlineMeeting.id (<c>MsGraphResourceId</c>) and
    /// the organizer's AAD oid, then register the transcript fetcher
    /// to poll <c>/users/{organizerOid}/onlineMeetings/{meetingId}/transcripts</c>
    /// until Microsoft's post-meeting transcript materializes.
    /// </summary>
    protected override async Task OnTeamsMeetingEndAsync(
        MeetingEndEventDetails meeting,
        ITurnContext<IEventActivity> turnContext,
        CancellationToken cancellationToken)
    {
        var activity = turnContext.Activity;
        var chatThreadId = activity.Conversation?.Id;
        if (string.IsNullOrWhiteSpace(chatThreadId))
        {
            await base.OnTeamsMeetingEndAsync(meeting, turnContext, cancellationToken);
            return;
        }

        string? canonicalMeetingId = TryGetChannelData(activity)?.Meeting?.Id ?? meeting?.Id?.ToString();
        string? organizerOid = null;
        string? subject = meeting?.Title?.Trim();
        string? organizerName = null;

        try
        {
            var info = await TeamsInfo.GetMeetingInfoAsync(turnContext, cancellationToken: cancellationToken);
            organizerOid = info?.Organizer?.AadObjectId;
            organizerName = info?.Organizer?.Name;
            var infoMid = info?.Details?.MsGraphResourceId;
            if (!string.IsNullOrWhiteSpace(infoMid)) canonicalMeetingId = infoMid;
            if (string.IsNullOrWhiteSpace(subject)) subject = info?.Details?.Title;
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex,
                "TeamsInfo.GetMeetingInfoAsync failed inside OnTeamsMeetingEndAsync for ChatThreadId={ChatThreadId}",
                chatThreadId);
        }

        _logger.LogInformation(
            "OnTeamsMeetingEndAsync ChatThreadId={ChatThreadId} CanonicalMeetingId={MeetingId} Subject={Subject} OrganizerOid={OrganizerOid}",
            chatThreadId,
            canonicalMeetingId ?? "(null)",
            subject ?? "(null)",
            organizerOid ?? "(null)");

        // Emit meeting.ended envelope.
        try
        {
            await _dispatcher.PublishAsync(
                new AlfredEventEnvelope
                {
                    EventType = AlfredEventTypes.MeetingEnded,
                    EventId = Guid.NewGuid().ToString("N"),
                    Ts = DateTimeOffset.UtcNow.ToString("O"),
                    MeetingRef = new MeetingRef
                    {
                        MeetingId = canonicalMeetingId ?? chatThreadId,
                        MeetingChatThreadId = chatThreadId,
                        Subject = subject,
                        Organizer = !string.IsNullOrWhiteSpace(organizerOid)
                            ? new SenderRef { AadId = organizerOid, DisplayName = organizerName }
                            : null,
                    },
                    ConversationReferenceId = chatThreadId,
                    Payload = new MeetingLifecyclePayload
                    {
                        Subject = subject,
                        ActualEndUtc = meeting?.EndTime.ToString("O"),
                    },
                },
                cancellationToken);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex,
                "Failed to publish meeting.ended for ChatThreadId={ChatThreadId}",
                chatThreadId);
        }

        // Register transcript fetcher with the canonical IDs. Without
        // both, Graph returns 404 "3004: Specified meeting is not found".
        if (!string.IsNullOrWhiteSpace(canonicalMeetingId)
            && !string.IsNullOrWhiteSpace(organizerOid))
        {
            _transcriptFetcher.Register(
                botCallId: canonicalMeetingId!,
                organizerOid: organizerOid!,
                meetingChatThreadId: chatThreadId,
                // Look back briefly — transcripts typically land within a
                // few minutes of meeting end. -1h is generous.
                registeredAtUtc: DateTimeOffset.UtcNow.AddHours(-1));
            _logger.LogInformation(
                "Registered transcript fetcher from OnTeamsMeetingEndAsync MeetingId={MeetingId} OrganizerOid={OrganizerOid}",
                canonicalMeetingId, organizerOid);
        }
        else
        {
            _logger.LogWarning(
                "Cannot register transcript fetcher from OnTeamsMeetingEndAsync — missing CanonicalMeetingId={MeetingId} or OrganizerOid={OrganizerOid}",
                canonicalMeetingId ?? "(null)",
                organizerOid ?? "(null)");
        }

        await base.OnTeamsMeetingEndAsync(meeting, turnContext, cancellationToken);
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
                _ = Task.Run(() => TryAutoJoinMeetingChatAsync(
                    chatThreadId!,
                    "added_to_meeting_chat",
                    activity.From?.AadObjectId ?? activity.From?.Id));

                // The person who added Alfred is the strongest client
                // routing signal (PLAN.md candidate #1).
                if (!string.IsNullOrWhiteSpace(activity.From?.AadObjectId))
                {
                    await TryBindClientRouteAsync(
                        turnContext,
                        chatThreadId!,
                        meetingId: null,
                        new[]
                        {
                            new ClientIdentityCandidate
                            {
                                AadObjectId = activity.From!.AadObjectId,
                                DisplayName = activity.From.Name,
                                Source = "installer",
                            },
                        },
                        cancellationToken);
                }
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
    private async Task TryAutoJoinMeetingChatAsync(
        string chatThreadId,
        string source,
        string? organizerOid)
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
            if (string.IsNullOrWhiteSpace(organizerOid))
            {
                _logger.LogWarning(
                    "Auto-join skipped: organizer/user OID is required to synthesize a Teams meeting URL. Thread={Thread} Source={Source}",
                    chatThreadId,
                    source);
                return;
            }

            var joinUrl = ChannelMeetingJoinUrls.Build(
                chatThreadId,
                tenantId!,
                organizerOid!);

            _logger.LogInformation(
                "Auto-joining meeting chat thread={Thread} source={Source} organizerOid={OrganizerOid}",
                chatThreadId, source, organizerOid);

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
        var ts = (activity.Timestamp ?? DateTimeOffset.UtcNow).UtcDateTime.ToString("o");
        var sender = new SenderRef
        {
            AadId = activity.From?.AadObjectId ?? activity.From?.Id,
            DisplayName = activity.From?.Name,
        };
        var autoJoinOrganizerOid = sender.AadId;
        var fromBot = activity.From?.Role == "bot";
        var messageId = activity.Id ?? Guid.NewGuid().ToString("N");
        var html = activity.Attachments?.FirstOrDefault(a => a.ContentType == "text/html")?.Content?.ToString();

        if (string.Equals(conversationKind, "channel", StringComparison.Ordinal)
            && !string.IsNullOrWhiteSpace(teamId)
            && !string.IsNullOrWhiteSpace(channelId))
        {
            var channelPayload = new ChannelMessagePayload
            {
                Sender = sender,
                Text = activity.Text,
                Html = html,
                TimestampUtc = ts,
                ReplyToMessageId = activity.ReplyToId,
                IsRoot = string.IsNullOrWhiteSpace(activity.ReplyToId),
                FromBot = fromBot,
            };
            await _dispatcher.PublishAsync(
                new AlfredEventEnvelope
                {
                    EventType = AlfredEventTypes.ChannelMessageCreated,
                    EventId = Guid.NewGuid().ToString("N"),
                    Ts = ts,
                    ChannelRef = new ChannelRef
                    {
                        TeamId = teamId!,
                        ChannelId = channelId!,
                        ThreadId = chatThreadId,
                        MessageId = messageId,
                    },
                    ConversationReferenceId = chatThreadId,
                    Payload = channelPayload,
                },
                cancellationToken);

            // Bot Framework delivers channel activities even when OnMembersAddedAsync didn't fire.
            // Ensure the attachment exists with friendly display names. Idempotent on existing names.
            await EnsureChannelAttachedAsync(
                turnContext, teamId!, channelId!, channelData, cancellationToken);
        }
        else
        {
            // Contract: meeting_id is the Graph onlineMeeting.id, not the chat
            // thread id. Resolve once per chat thread (cached).
            var canonicalMeetingId = await _metadataResolver.ResolveCanonicalMeetingIdAsync(
                chatThreadId, cancellationToken) ?? chatThreadId;

            // Use Bot Framework's TeamsInfo helper to pull the canonical
            // Graph meeting id, organizer, and subject for this meeting
            // chat. This is the SAME pattern Microsoft's official
            // meetings-transcription sample uses
            // (OfficeDev/Microsoft-Teams-Samples
            // /samples/meetings-transcription/csharp/.../TranscriptionBot.cs),
            // and it works with our RSC-only permissions because
            // TeamsInfo goes through the Bot Framework Teams extension
            // — not raw Graph endpoints that need Chat.ReadBasic.
            //
            // Net effect when this fires (first @-mention of Alfred in
            // any +Apps meeting):
            //   1. Subject populated  → UI shows the real meeting name.
            //   2. Organizer captured → fetch_meeting_transcript has the
            //      user id it needs for /users/{oid}/onlineMeetings/{id}.
            //   3. Canonical meeting id mapped from the chat thread id
            //      → /v2/meetings registry no longer collapses both ids
            //      into the chat-thread fallback.
            //   4. Transcript fetcher auto-registered with the right
            //      (meetingId, organizerOid) — when the meeting ends
            //      with R+T on, the bot pulls the transcript itself.
            if (_publishedMeetingCreated.TryAdd(chatThreadId, 1))
            {
                string? subject = null;
                string? graphMeetingId = null;
                SenderRef? organizer = null;

                try
                {
                    var meetingInfo = await TeamsInfo.GetMeetingInfoAsync(
                        turnContext, cancellationToken: cancellationToken);
                    graphMeetingId = meetingInfo?.Details?.MsGraphResourceId;
                    subject = meetingInfo?.Details?.Title?.Trim();
                    var orgAadId = meetingInfo?.Organizer?.AadObjectId;
                    if (!string.IsNullOrWhiteSpace(orgAadId))
                    {
                        autoJoinOrganizerOid = orgAadId;
                        organizer = new SenderRef
                        {
                            AadId = orgAadId,
                            DisplayName = meetingInfo!.Organizer.Name,
                        };
                    }
                    _logger.LogInformation(
                        "TeamsInfo.GetMeetingInfoAsync result ChatThreadId={ChatThreadId} MsGraphResourceId={MsGraphResourceId} Title={Title} OrganizerOid={OrganizerOid} OrganizerName={OrganizerName} ConversationName={ConvName}",
                        chatThreadId,
                        graphMeetingId ?? "(null)",
                        subject ?? "(null)",
                        orgAadId ?? "(null)",
                        meetingInfo?.Organizer?.Name ?? "(null)",
                        activity.Conversation?.Name ?? "(null)");
                }
                catch (Exception ex)
                {
                    _logger.LogWarning(ex,
                        "TeamsInfo.GetMeetingInfoAsync failed for ChatThreadId={ChatThreadId} ConversationName={ConvName}; falling back",
                        chatThreadId, activity.Conversation?.Name ?? "(null)");
                }

                // Fallback chain for subject: TeamsInfo.Details.Title
                // (best, post-fix) → activity.Conversation.Name (often
                // the meeting title for +Apps installs) → null.
                if (string.IsNullOrWhiteSpace(subject))
                {
                    var convName = activity.Conversation?.Name?.Trim();
                    if (!string.IsNullOrWhiteSpace(convName))
                    {
                        subject = convName;
                    }
                }

                // Use the canonical Graph meeting id if TeamsInfo gave
                // one, otherwise stick with the chat-thread fallback we
                // already resolved (canonicalMeetingId).
                var resolvedMeetingId = !string.IsNullOrWhiteSpace(graphMeetingId)
                    ? graphMeetingId!
                    : canonicalMeetingId;

                if (organizer is null && !string.IsNullOrWhiteSpace(sender.AadId))
                {
                    // Last-resort organizer fallback: the user who just
                    // typed. They're not necessarily the organizer, but
                    // populating SOMETHING beats null on the dossier.
                    organizer = new SenderRef
                    {
                        AadId = sender.AadId,
                        DisplayName = sender.DisplayName,
                    };
                }

                try
                {
                    await _dispatcher.PublishAsync(
                        new AlfredEventEnvelope
                        {
                            EventType = AlfredEventTypes.MeetingCreated,
                            EventId = Guid.NewGuid().ToString("N"),
                            Ts = DateTimeOffset.UtcNow.ToString("O"),
                            MeetingRef = new MeetingRef
                            {
                                MeetingId = resolvedMeetingId,
                                MeetingChatThreadId = chatThreadId,
                                Subject = subject,
                                Organizer = organizer,
                            },
                            ConversationReferenceId = chatThreadId,
                            Payload = new MeetingLifecyclePayload
                            {
                                Subject = subject,
                                Organizer = organizer,
                            },
                        },
                        cancellationToken);
                }
                catch (Exception ex)
                {
                    _logger.LogWarning(ex,
                        "Failed to emit meeting.created from Bot Framework activity for ChatThreadId={ChatThreadId}",
                        chatThreadId);
                    _publishedMeetingCreated.TryRemove(chatThreadId, out _);
                }

                // Eagerly register the transcript fetcher so it's polling
                // the moment a transcript appears. Idempotent — Register
                // dedups on botCallId. Only meaningful when we have both
                // the canonical meeting id AND organizer (the user-scoped
                // Graph URL needs both).
                if (!string.IsNullOrWhiteSpace(graphMeetingId)
                    && organizer?.AadId is { } orgOid
                    && !string.IsNullOrWhiteSpace(orgOid))
                {
                    _transcriptFetcher.Register(
                        botCallId: graphMeetingId!,
                        organizerOid: orgOid,
                        meetingChatThreadId: chatThreadId,
                        // Look back 24h so an already-completed meeting
                        // whose transcript landed BEFORE this @-mention
                        // still gets picked up.
                        registeredAtUtc: DateTimeOffset.UtcNow.AddHours(-24));
                    _logger.LogInformation(
                        "Registered transcript fetcher from Bot Framework activity MeetingId={MeetingId} OrganizerOid={OrganizerOid} ChatThreadId={ChatThreadId}",
                        graphMeetingId, orgOid, chatThreadId);
                }
            }

            var meetingPayload = new MeetingChatPayload
            {
                MessageId = messageId,
                Sender = sender,
                Text = activity.Text,
                Html = html,
                TimestampUtc = ts,
                ReplyToMessageId = activity.ReplyToId,
                FromBot = fromBot,
            };
            await _dispatcher.PublishAsync(
                new AlfredEventEnvelope
                {
                    EventType = AlfredEventTypes.MeetingChatCreated,
                    EventId = Guid.NewGuid().ToString("N"),
                    Ts = ts,
                    MeetingRef = new MeetingRef
                    {
                        MeetingId = canonicalMeetingId,
                        MeetingChatThreadId = chatThreadId,
                    },
                    ConversationReferenceId = chatThreadId,
                    Payload = meetingPayload,
                },
                cancellationToken);

            // Candidate order per PLAN.md: organizer, then first non-bot
            // sender. (The installer candidate fires in OnMembersAddedAsync.)
            var routeCandidates = new List<ClientIdentityCandidate>(2);
            if (!string.IsNullOrWhiteSpace(autoJoinOrganizerOid))
            {
                routeCandidates.Add(new ClientIdentityCandidate
                {
                    AadObjectId = autoJoinOrganizerOid,
                    Source = "organizer",
                });
            }
            if (!fromBot && !string.IsNullOrWhiteSpace(sender.AadId)
                && !string.Equals(sender.AadId, autoJoinOrganizerOid, StringComparison.OrdinalIgnoreCase))
            {
                routeCandidates.Add(new ClientIdentityCandidate
                {
                    AadObjectId = sender.AadId,
                    DisplayName = sender.DisplayName,
                    Source = "sender",
                });
            }
            if (routeCandidates.Count > 0)
            {
                await TryBindClientRouteAsync(
                    turnContext, chatThreadId, canonicalMeetingId, routeCandidates, cancellationToken);
            }

            // When a meeting chat carries channel context (channel meeting), emit meeting.linked
            // once per (chatThreadId, teamId, channelId) so consumers can roll this meeting under
            // its parent channel.
            if (!string.IsNullOrWhiteSpace(teamId)
                && !string.IsNullOrWhiteSpace(channelId)
                && !string.Equals(chatThreadId, channelId, StringComparison.Ordinal))
            {
                var linkKey = string.Join("|", chatThreadId, teamId, channelId);
                if (_publishedLinks.TryAdd(linkKey, 1))
                {
                    var channelLink = new ChannelLink
                    {
                        TeamId = teamId!,
                        ChannelId = channelId!,
                        LinkedAtUtc = DateTimeOffset.UtcNow.ToString("O"),
                        LinkedSource = "bot_framework_channeldata",
                    };
                    await _dispatcher.PublishAsync(
                        new AlfredEventEnvelope
                        {
                            EventType = AlfredEventTypes.MeetingLinked,
                            EventId = Guid.NewGuid().ToString("N"),
                            Ts = DateTimeOffset.UtcNow.ToString("O"),
                            MeetingRef = new MeetingRef
                            {
                                MeetingId = canonicalMeetingId,
                                MeetingChatThreadId = chatThreadId,
                                ChannelLink = channelLink,
                            },
                            ConversationReferenceId = chatThreadId,
                            Payload = new MeetingLinkedPayload { LinkedSource = "bot_framework_channeldata" },
                        },
                        cancellationToken);
                }
            }
        }

        // If Alfred was @-mentioned with a "link to <channel-name>" directive, handle it.
        if (WasBotMentioned(activity))
        {
            await TryHandleMeetingLinkCommandAsync(turnContext, chatThreadId, cancellationToken);
        }

        // If Alfred was @-mentioned in any meeting chat AND isn't already in the call, auto-join.
        if (LooksLikeMeetingChat(chatThreadId) && WasBotMentioned(activity))
        {
            _ = Task.Run(() => TryAutoJoinMeetingChatAsync(
                chatThreadId,
                "at_mention",
                autoJoinOrganizerOid));
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

    // ---- meeting -> channel link command --------------------------------

    /// <summary>
    /// Matches the operator's chat command that says "this meeting
    /// belongs to the alfred_test channel". Accepted phrasings (all
    /// after stripping @-mentions and #-prefixes):
    ///   "link to alfred_test"
    ///   "link this to alfred_test"
    ///   "link this meeting to alfred_test"
    ///   "this meeting is for alfred_test"
    ///   "this is for alfred_test"
    ///   "associate with alfred_test"
    ///   "channel: alfred_test"
    /// The captured group is the channel display name; trimmed and
    /// case-insensitively matched against the channel-attachment store.
    /// </summary>
    private static readonly System.Text.RegularExpressions.Regex _linkCommandRegex = new(
        @"(?ix)
          (?:
              link \s+ (?:this \s+(?:meeting \s+)? )? to \s+
            | this \s+ (?:meeting \s+)? is \s+ for \s+
            | this \s+ is \s+ for \s+
            | associate \s+ with \s+
            | channel \s* : \s*
          )
          \#? \s* (?<name> [\w\-_\. ]{1,80}? )
          \s* (?:[.,;!?]|$)
        ",
        System.Text.RegularExpressions.RegexOptions.Compiled);

    private async Task TryHandleMeetingLinkCommandAsync(
        ITurnContext<IMessageActivity> turnContext,
        string chatThreadId,
        CancellationToken cancellationToken)
    {
        var activity = turnContext.Activity;
        var raw = activity?.Text ?? string.Empty;
        var stripped = StripMentionsAndTags(raw);
        if (string.IsNullOrWhiteSpace(stripped)) return;

        var match = _linkCommandRegex.Match(stripped);
        if (!match.Success) return;

        var requested = match.Groups["name"].Value.Trim().TrimEnd('.', ',', ';', '!', '?');
        if (string.IsNullOrWhiteSpace(requested)) return;

        var attachments = _attachmentStore.List();
        var hits = attachments
            .Where(a => string.Equals(a.ChannelDisplayName, requested, StringComparison.OrdinalIgnoreCase))
            .ToList();

        if (hits.Count == 0)
        {
            await turnContext.SendActivityAsync(
                $"I'm not attached to a channel called **{requested}**. Add Alfred to that channel first, then try again.",
                cancellationToken: cancellationToken);
            return;
        }
        if (hits.Count > 1)
        {
            var disambiguation = string.Join(" / ",
                hits.Select(h => $"`{h.TeamDisplayName ?? "team?"} / {h.ChannelDisplayName}`"));
            await turnContext.SendActivityAsync(
                $"Multiple channels named **{requested}** are attached: {disambiguation}. Tell me which team it's in.",
                cancellationToken: cancellationToken);
            return;
        }

        var target = hits[0];
        var record = new MeetingChannelLinkRecord
        {
            ChatThreadId = chatThreadId,
            TeamId = target.TeamId,
            ChannelId = target.ChannelId,
            ChannelThreadId = target.ConversationThreadId ?? target.ChannelId,
            TeamDisplayName = target.TeamDisplayName,
            ChannelDisplayName = target.ChannelDisplayName,
            Source = "chat_command",
        };
        await _meetingLinks.UpsertAsync(record, cancellationToken);

        _logger.LogInformation(
            "Linked meeting ChatThreadId={ChatThreadId} -> Team='{Team}' / Channel='{Channel}' (via chat command)",
            chatThreadId, target.TeamDisplayName, target.ChannelDisplayName);

        var channelLink = new ChannelLink
        {
            TeamId = target.TeamId,
            TeamDisplayName = target.TeamDisplayName,
            ChannelId = target.ChannelId,
            ChannelDisplayName = target.ChannelDisplayName,
            ThreadId = target.ConversationThreadId ?? target.ChannelId,
            LinkedAtUtc = DateTimeOffset.UtcNow.ToString("O"),
            LinkedSource = "manual_command",
        };
        var canonicalMeetingId = await _metadataResolver.ResolveCanonicalMeetingIdAsync(
            chatThreadId, cancellationToken) ?? chatThreadId;
        await _dispatcher.PublishAsync(
            new AlfredEventEnvelope
            {
                EventType = AlfredEventTypes.MeetingLinked,
                EventId = Guid.NewGuid().ToString("N"),
                Ts = DateTimeOffset.UtcNow.ToString("O"),
                MeetingRef = new MeetingRef
                {
                    MeetingId = canonicalMeetingId,
                    MeetingChatThreadId = chatThreadId,
                    ChannelLink = channelLink,
                },
                ConversationReferenceId = chatThreadId,
                Payload = new MeetingLinkedPayload { LinkedSource = "manual_command" },
            },
            cancellationToken);

        await turnContext.SendActivityAsync(
            $"Linked this meeting to **{target.TeamDisplayName ?? "team"} / {target.ChannelDisplayName}**. " +
            "Future chat, audio STT, and post-meeting events from this meeting will roll up under that channel.",
            cancellationToken: cancellationToken);
    }

    /// <summary>
    /// Strips Bot Framework @-mention markup (the bot's name + leading
    /// '#' tags) so the link-command regex sees clean text. Bot
    /// Framework gives us text like "<at>Alfred Sandbox</at> link to
    /// #alfred_test" or HTML-escaped variants; activity.Text already
    /// drops the <at> tags but leaves the plain name in place. We do a
    /// best-effort whittle.
    /// </summary>
    private static string StripMentionsAndTags(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return string.Empty;
        var text = raw;
        // Some clients leave "<at>Name</at>"; strip the tags but keep the name out
        text = System.Text.RegularExpressions.Regex.Replace(text, @"<at[^>]*>[^<]*</at>", " ");
        // Common bot name forms; remove them so they don't get parsed as channel names
        foreach (var token in new[] { "alfred sandbox", "alfredsandbox", "alfred", "@alfred" })
        {
            text = System.Text.RegularExpressions.Regex.Replace(
                text, $@"\b{System.Text.RegularExpressions.Regex.Escape(token)}\b",
                " ", System.Text.RegularExpressions.RegexOptions.IgnoreCase);
        }
        // Collapse whitespace
        text = System.Text.RegularExpressions.Regex.Replace(text, @"\s+", " ").Trim();
        return text;
    }
}
