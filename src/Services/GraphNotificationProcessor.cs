using System.Collections.Concurrent;
using System.Net;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using Microsoft.AspNetCore.WebUtilities;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

public sealed partial class GraphNotificationProcessor
{
    private static readonly JsonSerializerOptions SerializerOptions = new(JsonSerializerDefaults.Web)
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly EventFanoutDispatcher _dispatcher;
    private readonly MeetingChatConfiguration _config;
    private readonly BotConfiguration _botConfig;
    private readonly IMeetingChatService _meetingChatService;
    private readonly IChannelAttachmentService _attachmentService;
    private readonly GraphApiClient _graphApiClient;
    private readonly GraphNotificationCrypto _crypto;
    private readonly GraphValidationTokenValidator _tokenValidator;
    private readonly TeamsCallingBotService _botService;
    private readonly TranscriberFactory _transcriberFactory;
    private readonly OfficialTranscriptFetcher _transcriptFetcher;
    private readonly GraphMetadataResolver _metadataResolver;
    private readonly ILogger<GraphNotificationProcessor> _logger;

    /// <summary>
    /// Per-<c>callId</c> latch so duplicate Graph notifications (or our
    /// own subscription renewals) never trigger more than one join
    /// attempt for the same Teams channel call.
    /// </summary>
    private readonly ConcurrentDictionary<string, byte> _attemptedJoins =
        new(StringComparer.Ordinal);

    /// <summary>
    /// Per-chat-thread latch so we emit <c>meeting.created</c> exactly
    /// once per "+Apps"-installed meeting chat we see. The first chat
    /// event on a previously-unseen meeting chat thread resolves
    /// subject/organizer/scheduled times via Graph and emits a
    /// metadata-rich <c>meeting.created</c> envelope so the sink's
    /// <c>/v2/meetings</c> registry shows a real subject instead of
    /// the raw chat-thread fallback id.
    /// </summary>
    private readonly ConcurrentDictionary<string, byte> _meetingCreatedEmitted =
        new(StringComparer.Ordinal);

    public GraphNotificationProcessor(
        EventFanoutDispatcher dispatcher,
        MeetingChatConfiguration config,
        BotConfiguration botConfig,
        IMeetingChatService meetingChatService,
        IChannelAttachmentService attachmentService,
        GraphApiClient graphApiClient,
        GraphNotificationCrypto crypto,
        GraphValidationTokenValidator tokenValidator,
        TeamsCallingBotService botService,
        TranscriberFactory transcriberFactory,
        OfficialTranscriptFetcher transcriptFetcher,
        GraphMetadataResolver metadataResolver,
        ILogger<GraphNotificationProcessor> logger)
    {
        _dispatcher = dispatcher;
        _config = config;
        _botConfig = botConfig;
        _meetingChatService = meetingChatService;
        _attachmentService = attachmentService;
        _graphApiClient = graphApiClient;
        _crypto = crypto;
        _tokenValidator = tokenValidator;
        _botService = botService;
        _transcriberFactory = transcriberFactory;
        _transcriptFetcher = transcriptFetcher;
        _metadataResolver = metadataResolver;
        _logger = logger;
    }

    public async Task ProcessAsync(string requestBody, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(requestBody))
        {
            return;
        }

        GraphNotificationEnvelope? envelope;
        try
        {
            envelope = JsonSerializer.Deserialize<GraphNotificationEnvelope>(requestBody, SerializerOptions);
        }
        catch (JsonException ex)
        {
            _logger.LogWarning(ex, "Failed to deserialize Graph notification payload.");
            return;
        }

        if (envelope is null || envelope.Value.Count == 0)
        {
            return;
        }

        if (!await _tokenValidator.ValidateAsync(envelope.ValidationTokens, cancellationToken))
        {
            _logger.LogWarning("Dropping Graph notification batch because validationTokens failed verification.");
            return;
        }

        foreach (var notification in envelope.Value)
        {
            try
            {
                if (!ValidateClientState(notification.ClientState))
                {
                    continue;
                }

                if (!string.IsNullOrWhiteSpace(notification.LifecycleEvent))
                {
                    await _meetingChatService.HandleLifecycleEventAsync(
                        notification.SubscriptionId,
                        notification.LifecycleEvent,
                        cancellationToken);
                    continue;
                }

                await ProcessMessageNotificationAsync(notification, cancellationToken);
            }
            catch (Exception ex)
            {
                _logger.LogError(
                    ex,
                    "Failed to process Graph notification subscription={SubscriptionId} resource={Resource}",
                    notification.SubscriptionId,
                    notification.Resource);
            }
        }
    }

    private async Task ProcessMessageNotificationAsync(
        GraphNotification notification,
        CancellationToken cancellationToken)
    {
        JsonDocument? document = null;

        try
        {
            document = await ResolveMessagePayloadAsync(notification, cancellationToken);
            var ctx = BuildMessageContext(notification, document);
            if (ctx is null)
            {
                return;
            }

            var isTracked = ctx.Value.IsChannel
                ? _meetingChatService.IsTrackedChannel(ctx.Value.TeamId ?? string.Empty, ctx.Value.ChannelId ?? string.Empty)
                : _meetingChatService.IsTrackedChatThread(ctx.Value.ChatThreadId);

            if (!isTracked)
            {
                _logger.LogDebug(
                    "Skipping Graph event for untracked source kind={Kind} thread={ChatThreadId} team={TeamId} channel={ChannelId}",
                    ctx.Value.IsChannel ? "channel" : "meeting_chat",
                    ctx.Value.ChatThreadId,
                    ctx.Value.TeamId,
                    ctx.Value.ChannelId);
                return;
            }

            var sender = new SenderRef { AadId = ctx.Value.SenderId, DisplayName = ctx.Value.SenderDisplayName };

            if (ctx.Value.IsChannel)
            {
                var eventType = ctx.Value.ChangeType.Equals("updated", StringComparison.OrdinalIgnoreCase)
                    ? AlfredEventTypes.ChannelMessageUpdated
                    : ctx.Value.ChangeType.Equals("deleted", StringComparison.OrdinalIgnoreCase)
                        ? AlfredEventTypes.ChannelMessageDeleted
                        : AlfredEventTypes.ChannelMessageCreated;

                var channelPayload = new ChannelMessagePayload
                {
                    Sender = sender,
                    Text = ctx.Value.Text,
                    Html = ctx.Value.Html,
                    TimestampUtc = ctx.Value.TimestampUtc,
                    ReplyToMessageId = ctx.Value.ReplyToMessageId,
                    IsRoot = string.IsNullOrWhiteSpace(ctx.Value.ReplyToMessageId),
                    FromBot = ctx.Value.FromBot,
                    Raw = ctx.Value.Raw,
                };
                await _dispatcher.PublishAsync(
                    new AlfredEventEnvelope
                    {
                        EventType = eventType,
                        EventId = Guid.NewGuid().ToString("N"),
                        Ts = ctx.Value.TimestampUtc,
                        ChannelRef = new ChannelRef
                        {
                            TeamId = ctx.Value.TeamId!,
                            ChannelId = ctx.Value.ChannelId!,
                            ThreadId = ctx.Value.ChatThreadId,
                            MessageId = ctx.Value.MessageId,
                        },
                        ConversationReferenceId = ctx.Value.ChatThreadId,
                        Payload = channelPayload,
                    },
                    cancellationToken);

                // Auto-join the channel meeting when Teams posts a callStartedEventMessageDetail.
                MaybeAutoJoinChannelMeeting(ctx.Value.TeamId!, ctx.Value.ChannelId!, ctx.Value.ChatThreadId, document?.RootElement);

                // Register post-meeting transcript fetch on meeting-export system payloads.
                MaybeFetchPostMeetingTranscript(ctx.Value.Text, ctx.Value.TeamId!, ctx.Value.ChannelId!, ctx.Value.ChatThreadId);
            }
            else
            {
                var eventType = ctx.Value.ChangeType.Equals("updated", StringComparison.OrdinalIgnoreCase)
                    ? AlfredEventTypes.MeetingChatUpdated
                    : ctx.Value.ChangeType.Equals("deleted", StringComparison.OrdinalIgnoreCase)
                        ? AlfredEventTypes.MeetingChatDeleted
                        : AlfredEventTypes.MeetingChatCreated;

                // Contract: meeting_id is the Graph onlineMeeting.id, never the
                // chat thread id. Resolve via /chats/{tid} → joinWebUrl → /users/
                // {org}/onlineMeetings. Falls back to the thread id if Graph
                // can't bridge (private meetings only — channel meetings don't
                // hit this branch anyway).
                var canonicalMeetingId = await _metadataResolver.ResolveCanonicalMeetingIdAsync(
                    ctx.Value.ChatThreadId, cancellationToken);
                if (string.IsNullOrWhiteSpace(canonicalMeetingId))
                {
                    _logger.LogWarning(
                        "Could not resolve canonical meeting_id for ChatThreadId={ChatThreadId}; emitting with thread id as fallback. Consumer dossiers may split.",
                        ctx.Value.ChatThreadId);
                }

                // First chat event on a "+Apps"-installed meeting chat we
                // haven't seen before: resolve subject + organizer +
                // scheduled times via Graph and emit a metadata-rich
                // meeting.created. Sink registry then shows a human-readable
                // subject instead of the raw chat-thread fallback id.
                // Best-effort; failures don't block the chat event.
                await MaybeEmitMeetingCreatedForChatThreadAsync(
                    ctx.Value.ChatThreadId, canonicalMeetingId, cancellationToken);

                var meetingPayload = new MeetingChatPayload
                {
                    MessageId = ctx.Value.MessageId,
                    Sender = sender,
                    Text = ctx.Value.Text,
                    Html = ctx.Value.Html,
                    TimestampUtc = ctx.Value.TimestampUtc,
                    ReplyToMessageId = ctx.Value.ReplyToMessageId,
                    FromBot = ctx.Value.FromBot,
                    Raw = ctx.Value.Raw,
                };
                await _dispatcher.PublishAsync(
                    new AlfredEventEnvelope
                    {
                        EventType = eventType,
                        EventId = Guid.NewGuid().ToString("N"),
                        Ts = ctx.Value.TimestampUtc,
                        MeetingRef = new MeetingRef
                        {
                            MeetingId = canonicalMeetingId ?? ctx.Value.ChatThreadId,
                            MeetingChatThreadId = ctx.Value.ChatThreadId,
                        },
                        ConversationReferenceId = ctx.Value.ChatThreadId,
                        Payload = meetingPayload,
                    },
                    cancellationToken);
            }
        }
        finally
        {
            document?.Dispose();
        }
    }

    /// <summary>
    /// Fires when Teams posts a <c>callStartedEventMessageDetail</c>
    /// system message into a channel Alfred is attached to. Synthesizes
    /// First chat event on a "+Apps"-installed meeting chat we haven't
    /// seen → resolve subject/organizer/scheduled times via Graph and
    /// emit a metadata-rich <c>meeting.created</c> envelope so the
    /// sink's <c>/v2/meetings</c> shows a real subject instead of the
    /// raw chat-thread fallback id. Idempotent (latched on chat thread
    /// id), best-effort (Graph failures don't throw — we just retry on
    /// the next chat event for that thread).
    /// </summary>
    private async Task MaybeEmitMeetingCreatedForChatThreadAsync(
        string chatThreadId,
        string? canonicalMeetingId,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(chatThreadId)) return;
        if (!_meetingCreatedEmitted.TryAdd(chatThreadId, 1)) return;

        try
        {
            var chat = await _metadataResolver.GetChatAsync(chatThreadId, cancellationToken);
            string? subject = null;
            string? scheduledStart = null;
            string? scheduledEnd = null;
            SenderRef? organizer = null;

            // We can only fetch the full onlineMeeting record when we
            // have BOTH the organizer's AAD id AND a canonical meeting
            // id (not a chat-thread fallback). The fallback case still
            // emits meeting.created with whatever we have.
            if (chat?.OrganizerAadId is not null
                && !string.IsNullOrWhiteSpace(canonicalMeetingId)
                && !string.Equals(canonicalMeetingId, chatThreadId, StringComparison.Ordinal))
            {
                var meeting = await _metadataResolver.GetOnlineMeetingAsync(
                    chat.OrganizerAadId, canonicalMeetingId, cancellationToken);
                if (meeting is not null)
                {
                    subject = meeting.Subject;
                    scheduledStart = meeting.ScheduledStartUtc;
                    scheduledEnd = meeting.ScheduledEndUtc;
                    organizer = new SenderRef
                    {
                        AadId = meeting.OrganizerAadId ?? chat.OrganizerAadId,
                        DisplayName = meeting.OrganizerDisplayName,
                    };
                }
            }

            var resolvedMeetingId = canonicalMeetingId ?? chatThreadId;
            var nowIso = DateTimeOffset.UtcNow.ToString("O");
            await _dispatcher.PublishAsync(
                new AlfredEventEnvelope
                {
                    EventType = AlfredEventTypes.MeetingCreated,
                    EventId = Guid.NewGuid().ToString("N"),
                    Ts = nowIso,
                    MeetingRef = new MeetingRef
                    {
                        MeetingId = resolvedMeetingId,
                        MeetingChatThreadId = chatThreadId,
                        Subject = subject,
                        Organizer = organizer,
                        ScheduledStartUtc = scheduledStart,
                        ScheduledEndUtc = scheduledEnd,
                    },
                    ConversationReferenceId = chatThreadId,
                    Payload = new MeetingLifecyclePayload
                    {
                        Subject = subject,
                        Organizer = organizer,
                        ScheduledStartUtc = scheduledStart,
                        ScheduledEndUtc = scheduledEnd,
                    },
                },
                cancellationToken);

            _logger.LogInformation(
                "Emitted meeting.created for ChatThreadId={ChatThreadId} MeetingId={MeetingId} Subject={Subject} Organizer={Organizer}",
                chatThreadId, resolvedMeetingId, subject ?? "(null)", organizer?.DisplayName ?? "(null)");
        }
        catch (Exception ex)
        {
            // Drop the latch so we retry on the next chat event for this thread.
            _meetingCreatedEmitted.TryRemove(chatThreadId, out _);
            _logger.LogWarning(ex,
                "Failed to emit meeting.created for ChatThreadId={ChatThreadId}; will retry on next chat event.",
                chatThreadId);
        }
    }

    /// <summary>
    /// Fires when Teams posts a <c>callStartedEventMessageDetail</c>
    /// system message into a channel Alfred is attached to. Synthesizes
    /// the channel-meeting join URL from the channel's threadId + the
    /// initiator's tenant/object id, then dispatches the join workflow
    /// fire-and-forget so this notification handler never blocks on the
    /// SDK round-trip.
    /// </summary>
    private void MaybeAutoJoinChannelMeeting(string teamId, string channelId, string channelThreadId, JsonElement? root)
    {
        if (!root.HasValue)
        {
            return;
        }

        var messageType = TryGetString(root, "messageType");
        if (!string.Equals(messageType, "systemEventMessage", StringComparison.Ordinal))
        {
            return;
        }

        if (!root.Value.TryGetProperty("eventDetail", out var detail) ||
            detail.ValueKind != JsonValueKind.Object)
        {
            return;
        }

        var detailType = detail.TryGetProperty("@odata.type", out var t) && t.ValueKind == JsonValueKind.String
            ? t.GetString()
            : null;

        // Only the call-started event triggers a join. Other call lifecycle
        // events (callEnded, callRecording, etc.) flow through as plain
        // chat events without bot action.
        if (string.IsNullOrWhiteSpace(detailType) ||
            !detailType.Contains("callStartedEventMessageDetail", StringComparison.Ordinal))
        {
            return;
        }

        var callId = detail.TryGetProperty("callId", out var c) && c.ValueKind == JsonValueKind.String
            ? c.GetString()
            : null;
        if (string.IsNullOrWhiteSpace(callId))
        {
            _logger.LogWarning(
                "callStartedEventMessageDetail with no callId on channel {ChannelId}; skipping auto-join.",
                channelId);
            return;
        }

        if (!_attemptedJoins.TryAdd(callId, 1))
        {
            _logger.LogDebug("Auto-join already attempted for callId={CallId}; skipping.", callId);
            return;
        }

        var attachment = _attachmentService.Get(teamId, channelId);
        if (attachment is not null && !attachment.AutoJoinEnabled)
        {
            _logger.LogInformation(
                "Auto-join disabled for team={TeamId} channel={ChannelId}; skipping callId={CallId}. Use POST /api/channels/{TeamId}/{ChannelId}/join for manual trigger.",
                teamId, channelId, callId, teamId, channelId);
            // Drop the latch so a future re-enable + retry can succeed.
            _attemptedJoins.TryRemove(callId, out _);
            return;
        }

        var initiatorOid = detail.TryGetProperty("initiator", out var init) &&
                           init.ValueKind == JsonValueKind.Object &&
                           init.TryGetProperty("user", out var user) &&
                           user.ValueKind == JsonValueKind.Object &&
                           user.TryGetProperty("id", out var uid) &&
                           uid.ValueKind == JsonValueKind.String
            ? uid.GetString()
            : null;

        // For SingleTenant deployments the bot tenant === team tenant.
        // Channel-meeting URLs use the team's tenant.
        var tenantId = _botConfig.TenantId;
        if (string.IsNullOrWhiteSpace(tenantId))
        {
            _logger.LogWarning(
                "Cannot auto-join callId={CallId}: Bot.TenantId is unset.", callId);
            return;
        }

        var joinUrl = ChannelMeetingJoinUrls.Build(
            channelThreadId,
            tenantId,
            initiatorOid ?? _botConfig.AppId ?? string.Empty);

        _logger.LogInformation(
            "Auto-joining channel meeting: team={TeamId} channel={ChannelId} thread={ThreadId} callId={CallId} initiator={InitiatorOid}",
            teamId, channelId, channelThreadId, callId, initiatorOid);

        // Fire-and-forget: don't block the notification handler on the
        // Graph Communications SDK call. Failure is logged; the next
        // call-started in the same channel will retry (different callId).
        _ = Task.Run(async () =>
        {
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
                        MeetingId = callId,
                        OrganizerTenantId = tenantId,
                        // Channel-attached bots have RSC consent equivalent to
                        // a roster invite; the BotAttendee check is moot.
                        BotAttendeePresent = true,
                    },
                    transcriber).ConfigureAwait(false);

                _logger.LogInformation(
                    "Auto-join workflow completed for callId={CallId}: SelectedMode={Mode} BotCallId={BotCallId} Deferred={Deferred} Message={Message}",
                    callId, result.SelectedJoinMode, result.CallId, result.Deferred, result.Message);

                await _attachmentService.RecordAutoJoinAttemptAsync(
                    teamId, channelId,
                    new AutoJoinAttempt
                    {
                        Ts = DateTimeOffset.UtcNow.ToString("O"),
                        Trigger = "auto",
                        Status = result.Deferred ? "deferred" : "success",
                        CallId = result.CallId,
                        SourceCallId = callId,
                    }).ConfigureAwait(false);

                if (!result.Deferred && !string.IsNullOrWhiteSpace(initiatorOid) && !string.IsNullOrWhiteSpace(result.CallId))
                {
                    _transcriptFetcher.Register(
                        botCallId: result.CallId!,
                        organizerOid: initiatorOid!,
                        meetingChatThreadId: channelThreadId,
                        registeredAtUtc: DateTimeOffset.UtcNow);
                }
            }
            catch (JoinWorkflowException jex)
            {
                _logger.LogError(jex,
                    "Auto-join workflow rejected for callId={CallId} on channel={ChannelId} code={ErrorCode}",
                    callId, channelId, jex.ErrorCode);
                await _attachmentService.RecordAutoJoinAttemptAsync(
                    teamId, channelId,
                    new AutoJoinAttempt
                    {
                        Ts = DateTimeOffset.UtcNow.ToString("O"),
                        Trigger = "auto",
                        Status = "failure",
                        ErrorCode = jex.ErrorCode,
                        ErrorMessage = jex.Message,
                        SourceCallId = callId,
                    }).ConfigureAwait(false);
                _attemptedJoins.TryRemove(callId, out _);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex,
                    "Auto-join failed for callId={CallId} on channel={ChannelId}",
                    callId, channelId);
                await _attachmentService.RecordAutoJoinAttemptAsync(
                    teamId, channelId,
                    new AutoJoinAttempt
                    {
                        Ts = DateTimeOffset.UtcNow.ToString("O"),
                        Trigger = "auto",
                        Status = "failure",
                        ErrorMessage = ex.Message,
                        SourceCallId = callId,
                    }).ConfigureAwait(false);
                _attemptedJoins.TryRemove(callId, out _);
            }
        });
    }

    private async Task<JsonDocument?> ResolveMessagePayloadAsync(
        GraphNotification notification,
        CancellationToken cancellationToken)
    {
        if (notification.EncryptedContent is not null)
        {
            return _crypto.DecryptPayload(notification.EncryptedContent);
        }

        if (string.Equals(notification.ChangeType, "deleted", StringComparison.OrdinalIgnoreCase))
        {
            return null;
        }

        var resource = notification.ResourceData?.OdataId ?? notification.Resource;
        if (string.IsNullOrWhiteSpace(resource))
        {
            return null;
        }

        return await _graphApiClient.GetResourceAsync(resource, cancellationToken);
    }

    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, byte> _attemptedTranscriptFetches =
        new(StringComparer.Ordinal);

    /// <summary>
    /// Extracts <c>(callId, organizerOid)</c> from either supported Teams
    /// meeting system-message shape (JSON with <c>scopeId</c>/<c>callId</c>
    /// or the <c>&lt;URIObject type="Video.2/CallRecording.1"&gt;</c> XML
    /// form). Returns nulls when the payload isn't a recognized shape.
    /// </summary>
    private static (string? callId, string? organizerOid) ExtractMeetingMetadata(string text)
    {
        var trimmed = text.TrimStart();

        if (trimmed.StartsWith("{", StringComparison.Ordinal))
        {
            try
            {
                using var doc = JsonDocument.Parse(text);
                if (doc.RootElement.ValueKind != JsonValueKind.Object) return (null, null);
                if (!doc.RootElement.TryGetProperty("callId", out var cId) ||
                    cId.ValueKind != JsonValueKind.String)
                {
                    return (null, null);
                }
                var callId = cId.GetString();
                string? organizerOid = null;
                if (doc.RootElement.TryGetProperty("meetingOrganizerId", out var mo) &&
                    mo.ValueKind == JsonValueKind.String)
                {
                    var raw = mo.GetString();
                    organizerOid = raw is not null && raw.StartsWith("8:orgid:", StringComparison.Ordinal)
                        ? raw.Substring("8:orgid:".Length)
                        : raw;
                }
                return (callId, organizerOid);
            }
            catch
            {
                return (null, null);
            }
        }

        if (trimmed.StartsWith("<URIObject", StringComparison.Ordinal))
        {
            var callMatch = _callIdRegex.Match(text);
            var orgMatch = _organizerRegex.Match(text);
            var callId = callMatch.Success ? callMatch.Groups[1].Value : null;
            var organizerOid = orgMatch.Success ? orgMatch.Groups[1].Value : null;
            return (callId, organizerOid);
        }

        return (null, null);
    }

    /// <summary>
    /// True when this chat-message text is a Teams meeting lifecycle
    /// system payload. Teams emits two distinct shapes into channel
    /// chat streams, both of which we want to peel off from the
    /// human-chat envelope path:
    /// <list type="bullet">
    /// <item>JSON: <c>{"scopeId":"...","callId":"..."}</c> — call
    /// started / ended / exported to ODSP.</item>
    /// <item>XML: <c>&lt;URIObject type="Video.2/CallRecording.1"
    /// ...&gt;...&lt;/URIObject&gt;</c> — recording / transcript
    /// chunk-finished / call-ended notification with embedded
    /// <c>&lt;Id type="callId"&gt;</c> and
    /// <c>&lt;MeetingOrganizerId&gt;</c> elements.</item>
    /// </list>
    /// </summary>
    private static bool LooksLikeTeamsMeetingSystemPayload(string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return false;
        var trimmed = text.TrimStart();
        if (trimmed.StartsWith("{", StringComparison.Ordinal))
        {
            try
            {
                using var doc = JsonDocument.Parse(text);
                if (doc.RootElement.ValueKind != JsonValueKind.Object) return false;
                return doc.RootElement.TryGetProperty("scopeId", out _) &&
                       doc.RootElement.TryGetProperty("callId", out _);
            }
            catch
            {
                return false;
            }
        }
        if (trimmed.StartsWith("<URIObject", StringComparison.Ordinal))
        {
            // Cheap content check: every Teams call/recording URIObject
            // we've seen carries Video.2/CallRecording.1 in its type attr.
            return trimmed.Contains("type=\"Video.2/CallRecording.1\"", StringComparison.Ordinal) ||
                   trimmed.Contains("type='Video.2/CallRecording.1'", StringComparison.Ordinal);
        }
        return false;
    }

    private static readonly System.Text.RegularExpressions.Regex _callIdRegex = new(
        @"<Id\s+type=""callId""\s+value=""([^""]+)""",
        System.Text.RegularExpressions.RegexOptions.Compiled);

    private static readonly System.Text.RegularExpressions.Regex _organizerRegex = new(
        @"<MeetingOrganizerId\s+value=""8:orgid:([^""]+)""",
        System.Text.RegularExpressions.RegexOptions.Compiled);

    /// <summary>
    /// When Teams posts a meeting lifecycle system message (call ended,
    /// recording exported, transcript ready) into an attached channel,
    /// the message body is a JSON payload with <c>scopeId</c>,
    /// <c>callId</c>, and <c>meetingOrganizerId</c>. Use that to
    /// register a post-meeting Graph transcript fetch even if Alfred
    /// never joined the call itself — the
    /// <c>installedToOnlineMeetings/getAllTranscripts</c> endpoint is
    /// gated by the channel's team-level install, not by whether the
    /// bot was a participant.
    /// </summary>
    private void MaybeFetchPostMeetingTranscript(string? text, string teamId, string channelId, string channelThreadId)
    {
        if (string.IsNullOrWhiteSpace(text))
        {
            return;
        }

        var (callId, organizerOid) = ExtractMeetingMetadata(text!);
        if (string.IsNullOrWhiteSpace(callId) || string.IsNullOrWhiteSpace(organizerOid))
        {
            return;
        }

        // Idempotent per callId — Teams emits several system messages per
        // meeting (start, end, recording exported, etc.) and we only need
        // to schedule one fetch per call.
        if (!_attemptedTranscriptFetches.TryAdd(callId!, 1))
        {
            return;
        }

        // Register with a small look-back so transcripts created during
        // the call (before this "export" message lands) still match the
        // fetcher's createdDateTime filter.
        var registerAt = DateTimeOffset.UtcNow.AddMinutes(-30);

        _logger.LogInformation(
            "Scheduling post-meeting transcript fetch from channel system event callId={CallId} organizer={Oid} team={TeamId} channel={ChannelId}",
            callId, organizerOid, teamId, channelId);

        _transcriptFetcher.Register(
            botCallId: callId!,
            organizerOid: organizerOid!,
            meetingChatThreadId: channelThreadId,
            registeredAtUtc: registerAt);
    }

    private readonly record struct MessageContext(
        string ChangeType,
        string ChatThreadId,
        string MessageId,
        string? Text,
        string? Html,
        string? SenderId,
        string? SenderDisplayName,
        string TimestampUtc,
        bool FromBot,
        bool IsChannel,
        string? TeamId,
        string? ChannelId,
        string? ReplyToMessageId,
        Dictionary<string, object?>? Raw
    );

    private MessageContext? BuildMessageContext(GraphNotification notification, JsonDocument? document)
    {
        var root = document?.RootElement;

        var resourceForParsing = notification.ResourceData?.OdataId ?? notification.Resource;
        var (teamIdFromResource, channelIdFromResource) = ParseChannelIds(resourceForParsing);
        var teamId = TryGetNestedString(root, "channelIdentity", "teamId") ?? teamIdFromResource;
        var channelId = TryGetNestedString(root, "channelIdentity", "channelId") ?? channelIdFromResource;
        var isChannel = !string.IsNullOrWhiteSpace(teamId) && !string.IsNullOrWhiteSpace(channelId);

        string? chatThreadId;
        if (isChannel)
        {
            chatThreadId = $"19:{channelId}@thread.tacv2";
        }
        else
        {
            chatThreadId = TryGetString(root, "chatId")
                ?? ParseChatThreadId(notification.ResourceData?.OdataId)
                ?? ParseChatThreadId(notification.Resource);
        }

        if (string.IsNullOrWhiteSpace(chatThreadId))
        {
            _logger.LogDebug("Graph notification did not include a chat thread id.");
            return null;
        }

        var messageId = TryGetString(root, "id")
            ?? notification.ResourceData?.Id
            ?? ParseMessageId(notification.ResourceData?.OdataId)
            ?? ParseMessageId(notification.Resource)
            ?? Guid.NewGuid().ToString("N");

        var timestamp = TryGetString(root, "lastModifiedDateTime")
            ?? TryGetString(root, "createdDateTime")
            ?? DateTimeOffset.UtcNow.UtcDateTime.ToString("o");

        var html = TryGetNestedString(root, "body", "content");
        var text = html is null ? TryGetNestedString(root, "body", "content") : StripHtml(html);
        if (string.IsNullOrWhiteSpace(text))
        {
            text = TryGetString(root, "summary");
        }

        var senderId = TryGetNestedString(root, "from", "user", "id")
            ?? TryGetNestedString(root, "from", "application", "id");
        var senderDisplayName = TryGetNestedString(root, "from", "user", "displayName")
            ?? TryGetNestedString(root, "from", "application", "displayName");
        var senderApplicationId = TryGetNestedString(root, "from", "application", "id");

        return new MessageContext(
            ChangeType: notification.ChangeType ?? "created",
            ChatThreadId: chatThreadId,
            MessageId: messageId,
            Text: text,
            Html: html,
            SenderId: senderId,
            SenderDisplayName: senderDisplayName,
            TimestampUtc: timestamp,
            FromBot: string.Equals(senderApplicationId, _botConfig.AppId, StringComparison.OrdinalIgnoreCase)
                || string.Equals(senderId, _botConfig.AppId, StringComparison.OrdinalIgnoreCase),
            IsChannel: isChannel,
            TeamId: teamId,
            ChannelId: channelId,
            ReplyToMessageId: TryGetString(root, "replyToId"),
            Raw: root.HasValue
                ? JsonSerializer.Deserialize<Dictionary<string, object?>>(root.Value.GetRawText(), SerializerOptions)
                : BuildMinimalRaw(notification, chatThreadId, messageId)
        );
    }

    /// <summary>
    /// Parses <c>(teamId, channelId)</c> out of a Graph notification resource
    /// path of the shape <c>teams/{teamId}/channels/{channelId}/messages/{id}</c>.
    /// Returns <c>(null, null)</c> when the resource is not a channel-messages
    /// path.
    /// </summary>
    private static (string? TeamId, string? ChannelId) ParseChannelIds(string? resource)
    {
        if (string.IsNullOrWhiteSpace(resource))
        {
            return (null, null);
        }

        var path = ExtractPath(resource);
        var segments = path.Split('/', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);

        string? teamId = null;
        string? channelId = null;
        for (var index = 0; index < segments.Length - 1; index++)
        {
            if (string.Equals(segments[index], "teams", StringComparison.OrdinalIgnoreCase))
            {
                teamId = Uri.UnescapeDataString(segments[index + 1]);
            }
            else if (string.Equals(segments[index], "channels", StringComparison.OrdinalIgnoreCase))
            {
                channelId = Uri.UnescapeDataString(segments[index + 1]);
            }
        }

        return (teamId, channelId);
    }

    private bool ValidateClientState(string? clientState)
    {
        if (string.IsNullOrWhiteSpace(_config.ChatSubscriptionClientStateSecret))
        {
            return true;
        }

        if (!string.Equals(clientState, _config.ChatSubscriptionClientStateSecret, StringComparison.Ordinal))
        {
            _logger.LogWarning("Dropping Graph notification with invalid clientState.");
            return false;
        }

        return true;
    }

    private static Dictionary<string, object?> BuildMinimalRaw(
        GraphNotification notification,
        string chatThreadId,
        string messageId) =>
        new()
        {
            ["resource"] = notification.Resource,
            ["change_type"] = notification.ChangeType,
            ["chat_id"] = chatThreadId,
            ["message_id"] = messageId,
        };

    private static List<Dictionary<string, object?>> DeserializeJsonList(JsonElement? root, string propertyName)
    {
        if (!root.HasValue || !root.Value.TryGetProperty(propertyName, out var property) || property.ValueKind != JsonValueKind.Array)
        {
            return [];
        }

        var result = new List<Dictionary<string, object?>>();
        foreach (var item in property.EnumerateArray())
        {
            var parsed = JsonSerializer.Deserialize<Dictionary<string, object?>>(item.GetRawText(), SerializerOptions);
            if (parsed is not null)
            {
                result.Add(parsed);
            }
        }

        return result;
    }

    private static string? TryGetString(JsonElement? root, string propertyName)
    {
        if (!root.HasValue || !root.Value.TryGetProperty(propertyName, out var property))
        {
            return null;
        }

        return property.ValueKind == JsonValueKind.String ? property.GetString() : property.GetRawText();
    }

    private static string? TryGetNestedString(JsonElement? root, params string[] path)
    {
        if (!root.HasValue)
        {
            return null;
        }

        var current = root.Value;
        foreach (var segment in path)
        {
            if (!current.TryGetProperty(segment, out var next))
            {
                return null;
            }

            current = next;
        }

        return current.ValueKind == JsonValueKind.String ? current.GetString() : current.GetRawText();
    }

    private static string? ParseChatThreadId(string? resource)
    {
        if (string.IsNullOrWhiteSpace(resource))
        {
            return null;
        }

        var path = ExtractPath(resource);
        var segments = path.Split('/', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        for (var index = 0; index < segments.Length - 1; index++)
        {
            if (string.Equals(segments[index], "chats", StringComparison.OrdinalIgnoreCase))
            {
                return Uri.UnescapeDataString(segments[index + 1]);
            }
        }

        return null;
    }

    private static string? ParseMessageId(string? resource)
    {
        if (string.IsNullOrWhiteSpace(resource))
        {
            return null;
        }

        var path = ExtractPath(resource);
        var segments = path.Split('/', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        for (var index = 0; index < segments.Length - 1; index++)
        {
            if (string.Equals(segments[index], "messages", StringComparison.OrdinalIgnoreCase))
            {
                return Uri.UnescapeDataString(segments[index + 1]);
            }
        }

        return null;
    }

    private static string ExtractPath(string resource)
    {
        if (Uri.TryCreate(resource, UriKind.Absolute, out var absolute))
        {
            return absolute.AbsolutePath;
        }

        return QueryHelpers.ParseQuery(resource).Count > 0
            ? resource.Split('?', 2)[0]
            : resource;
    }

    private static string? StripHtml(string? html)
    {
        if (string.IsNullOrWhiteSpace(html))
        {
            return html;
        }

        return CollapseWhitespaceRegex().Replace(HtmlTagRegex().Replace(System.Net.WebUtility.HtmlDecode(html), " "), " ").Trim();
    }

    [GeneratedRegex("<[^>]+>", RegexOptions.Compiled)]
    private static partial Regex HtmlTagRegex();

    [GeneratedRegex("\\s+", RegexOptions.Compiled)]
    private static partial Regex CollapseWhitespaceRegex();
}

public sealed record GraphNotificationEnvelope
{
    [JsonPropertyName("value")]
    public List<GraphNotification> Value { get; init; } = [];

    [JsonPropertyName("validationTokens")]
    public List<string>? ValidationTokens { get; init; }
}

public sealed record GraphNotification
{
    [JsonPropertyName("subscriptionId")]
    public string? SubscriptionId { get; init; }

    [JsonPropertyName("changeType")]
    public string? ChangeType { get; init; }

    [JsonPropertyName("resource")]
    public string? Resource { get; init; }

    [JsonPropertyName("clientState")]
    public string? ClientState { get; init; }

    [JsonPropertyName("tenantId")]
    public string? TenantId { get; init; }

    [JsonPropertyName("subscriptionExpirationDateTime")]
    public DateTimeOffset? SubscriptionExpirationDateTime { get; init; }

    [JsonPropertyName("lifecycleEvent")]
    public string? LifecycleEvent { get; init; }

    [JsonPropertyName("resourceData")]
    public GraphResourceData? ResourceData { get; init; }

    [JsonPropertyName("encryptedContent")]
    public GraphEncryptedContent? EncryptedContent { get; init; }
}

public sealed record GraphResourceData
{
    [JsonPropertyName("@odata.type")]
    public string? OdataType { get; init; }

    [JsonPropertyName("@odata.id")]
    public string? OdataId { get; init; }

    [JsonPropertyName("id")]
    public string? Id { get; init; }
}

public sealed record GraphEncryptedContent
{
    [JsonPropertyName("data")]
    public required string Data { get; init; }

    [JsonPropertyName("dataSignature")]
    public required string DataSignature { get; init; }

    [JsonPropertyName("dataKey")]
    public required string DataKey { get; init; }

    [JsonPropertyName("encryptionCertificateId")]
    public string? EncryptionCertificateId { get; init; }

    [JsonPropertyName("encryptionCertificateThumbprint")]
    public string? EncryptionCertificateThumbprint { get; init; }
}
