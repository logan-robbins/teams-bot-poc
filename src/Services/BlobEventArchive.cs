using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Azure.Identity;
using Azure.Storage.Blobs;
using Azure.Storage.Blobs.Models;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Configuration for the per-channel / per-meeting Azure Blob archive.
/// All Alfred events that flow through <see cref="EventFanoutDispatcher"/>
/// are also persisted as individual <c>.json</c> blobs so downstream
/// consumers don't have to ingest the POST fan-out to retain history.
/// </summary>
public sealed class BlobArchiveConfiguration
{
    /// <summary>
    /// Azure storage account connection string. Either this OR
    /// (<see cref="AccountUrl"/> with managed identity) must be set.
    /// </summary>
    public string? ConnectionString { get; set; }

    /// <summary>
    /// Blob service endpoint, e.g.
    /// <c>https://stalfreddisney.blob.core.windows.net</c>. Used together
    /// with the runtime's <c>DefaultAzureCredential</c> when no
    /// <see cref="ConnectionString"/> is provided.
    /// </summary>
    public string? AccountUrl { get; set; }

    /// <summary>Container that holds every Alfred archive blob.</summary>
    public string ContainerName { get; set; } = "alfred-events";

    /// <summary>
    /// Master switch for the v1-compat dual-write. When true and a v2
    /// envelope's channel id matches an entry in
    /// <see cref="V1CompatChannelIds"/>, the bot writes the v2 blob at
    /// its canonical v2 path AND a v1-format blob at the legacy v1 path
    /// so pre-v2 polling consumers (e.g. a Python bridge that polls
    /// <c>channels/{team}/{cid_sanitized}/chat.message/</c>) keep
    /// working through the cutover.
    /// </summary>
    public bool V1CompatEnabled { get; set; }

    /// <summary>
    /// Allow-list of channel ids that should receive the v1-compat
    /// dual-write while <see cref="V1CompatEnabled"/> is true. Empty
    /// list = no compat writes (zero overhead). Each entry is the raw
    /// Teams channel id (e.g. <c>19:abc@thread.tacv2</c>) — same
    /// format as what <c>ChannelRef.ChannelId</c> carries.
    /// </summary>
    public List<string> V1CompatChannelIds { get; set; } = new();

    public bool IsConfigured =>
        !string.IsNullOrWhiteSpace(ConnectionString) ||
        !string.IsNullOrWhiteSpace(AccountUrl);
}

/// <summary>
/// Writes Alfred event envelopes and post-meeting official transcripts
/// to Azure Blob Storage. Per-event blobs are pure JSON keyed by
/// team / channel / meeting and event type; the official transcript is
/// dual-written as clean plaintext and raw WebVTT. Sits parallel to
/// the <see cref="EventFanoutDispatcher"/>'s HTTP fan-out path: every
/// event the dispatcher publishes to a Python sink is also persisted
/// here so any consumer can replay history without a live HTTP listener.
/// </summary>
/// <remarks>
/// Path layout (mirrors the Microsoft Graph URL hierarchy where one
/// exists, with one folder per logical data type instead of per
/// event_type variant):
///   teams/{team_id}/channels/{channel_id}/messages/{utcTs}-{eventId}.json
///   teams/{team_id}/channels/{channel_id}/lifecycle/{utcTs}-{eventId}.json
///   meetings/{meeting_id}/messages/{utcTs}-{eventId}.json
///   meetings/{meeting_id}/live_transcript/{utcTs}-{eventId}.json
///   meetings/{meeting_id}/transcripts/{utcTs}-{eventId}.json   (meeting.transcript.official envelope)
///   meetings/{meeting_id}/transcripts/official.txt             (clean speaker-per-line plaintext)
///   meetings/{meeting_id}/transcripts/official.vtt             (raw WebVTT)
///   meetings/{meeting_id}/lifecycle/{utcTs}-{eventId}.json
///
/// Every <c>.json</c> blob is a pure alfred-v2 envelope — no preamble,
/// no markers, just <c>{ … }</c>. Consumers can <c>jq</c> them
/// directly. See <c>docs/retrieving-transcripts.md</c> for the
/// consumer contract.
///
/// Categories collapse the v2 <c>event_type</c> variants:
///   messages        ← channel.message.{created,updated,deleted}, meeting.chat.{created,updated,deleted}
///                     (mirrors Graph's /teams/{id}/channels/{id}/messages + /chats/{id}/messages)
///   live_transcript ← meeting.transcript.{partial,final}
///                     (THIS bot's Azure Speech STT from the real-time audio stream — distinct
///                      from Microsoft's "live captions" feature; the bot is the producer)
///   transcripts     ← meeting.transcript.official (envelope sits alongside the flat
///                     official.txt / official.vtt files; mirrors Graph's
///                     /onlineMeetings/{id}/transcripts callTranscript resource)
///   lifecycle       ← channel.{attached,detached}, meeting.{created,ended,linked,call.joined,call.left}
/// The precise <c>event_type</c> stays on the envelope JSON, so any
/// consumer that needs the subtype reads it from there.
///
/// Auth: prefers <see cref="BlobArchiveConfiguration.ConnectionString"/>
/// when set (account-key path, current sandbox state) and falls back to
/// <see cref="DefaultAzureCredential"/> against
/// <see cref="BlobArchiveConfiguration.AccountUrl"/> so we can swap to
/// managed identity later without code changes.
/// </remarks>
public sealed class BlobEventArchive
{
    internal static readonly JsonSerializerOptions EnvelopeJsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull,
        WriteIndented = true,
    };

    private static readonly Regex UnsafePathChars = new(@"[^a-zA-Z0-9\-_.]", RegexOptions.Compiled);

    private readonly BlobContainerClient? _container;
    private readonly ILogger<BlobEventArchive> _logger;
    private readonly bool _v1CompatEnabled;
    private readonly HashSet<string> _v1CompatChannelIds;

    public BlobEventArchive(BlobArchiveConfiguration config, ILogger<BlobEventArchive> logger)
    {
        _logger = logger;
        _v1CompatEnabled = config.V1CompatEnabled;
        _v1CompatChannelIds = new HashSet<string>(
            config.V1CompatChannelIds ?? new List<string>(),
            StringComparer.OrdinalIgnoreCase);

        if (!config.IsConfigured)
        {
            _logger.LogInformation(
                "BlobEventArchive is not configured (no connection string or account url). Skipping archive writes.");
            return;
        }

        BlobServiceClient service;
        if (!string.IsNullOrWhiteSpace(config.ConnectionString))
        {
            service = new BlobServiceClient(config.ConnectionString);
        }
        else
        {
            service = new BlobServiceClient(new Uri(config.AccountUrl!), new DefaultAzureCredential());
        }

        _container = service.GetBlobContainerClient(config.ContainerName);
        _logger.LogInformation(
            "BlobEventArchive ready: container={Container} endpoint={Endpoint} V1Compat={V1Compat} V1CompatChannels={V1Channels}",
            config.ContainerName, _container.Uri,
            _v1CompatEnabled, _v1CompatChannelIds.Count);
    }

    public bool IsEnabled => _container is not null;

    /// <summary>
    /// Fire-and-forget archive of an Alfred event envelope. Never throws —
    /// archive failures are logged and swallowed so they cannot impact the
    /// hot dispatch path that called us.
    /// </summary>
    public Task ArchiveEnvelopeAsync(AlfredEventEnvelope envelope, CancellationToken cancellationToken = default)
    {
        if (_container is null)
        {
            return Task.CompletedTask;
        }
        if (envelope is null)
        {
            return Task.CompletedTask;
        }

        return Task.Run(async () =>
        {
            try
            {
                var path = BuildEnvelopePath(envelope);
                var body = JsonSerializer.Serialize(envelope, EnvelopeJsonOptions);
                await UploadAsync(path, body, "application/json", cancellationToken);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex,
                    "BlobEventArchive envelope write failed EventType={EventType} EventId={EventId}",
                    envelope.EventType, envelope.EventId);
            }

            // V1-compat dual write: for channels explicitly listed in
            // BlobArchive:V1CompatChannelIds, also persist the envelope in
            // the legacy alfred-events-v1 shape at the legacy v1 path so
            // pre-v2 polling consumers (e.g. server.py bridges that watch
            // channels/{team}/{cid}/chat.message/) keep working through
            // the cutover. Only channel.message.* events are compat-written
            // because that's the only event family v1 bridges read.
            if (_v1CompatEnabled
                && envelope.ChannelRef is { } cref
                && !string.IsNullOrWhiteSpace(cref.ChannelId)
                && _v1CompatChannelIds.Contains(cref.ChannelId)
                && IsV1CompatChannelMessageEvent(envelope.EventType))
            {
                try
                {
                    var (v1Path, v1Body) = BuildV1CompatChannelMessage(envelope, cref);
                    await UploadAsync(v1Path, v1Body, "text/plain", cancellationToken);
                }
                catch (Exception ex)
                {
                    _logger.LogWarning(ex,
                        "BlobEventArchive v1-compat write failed EventType={EventType} EventId={EventId} ChannelId={ChannelId}",
                        envelope.EventType, envelope.EventId, cref.ChannelId);
                }
            }
        }, cancellationToken);
    }

    private static bool IsV1CompatChannelMessageEvent(string? eventType) =>
        eventType == AlfredEventTypes.ChannelMessageCreated
        || eventType == AlfredEventTypes.ChannelMessageUpdated
        || eventType == AlfredEventTypes.ChannelMessageDeleted;

    /// <summary>
    /// Build the legacy alfred-events-v1 blob (path + body) from a v2
    /// channel.message.* envelope. Matches the exact shape the bot was
    /// writing pre-v2 so polling consumers don't have to change. Body
    /// is a human-readable header line + <c>---ENVELOPE---</c> separator
    /// + v1-format JSON with flat top-level fields and flat payload
    /// sender_id / sender_display_name.
    /// </summary>
    private static (string Path, string Body) BuildV1CompatChannelMessage(
        AlfredEventEnvelope envelope, ChannelRef cref)
    {
        var ts = DateTimeOffset.TryParse(envelope.Ts, CultureInfo.InvariantCulture,
            DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
            out var parsed)
            ? parsed.ToString("yyyyMMddTHHmmssfffZ", CultureInfo.InvariantCulture)
            : DateTimeOffset.UtcNow.ToString("yyyyMMddTHHmmssfffZ", CultureInfo.InvariantCulture);
        var safeId = SanitizePathSegment(envelope.EventId ?? Guid.NewGuid().ToString("N"));
        var cidSanitized = SanitizePathSegment(cref.ChannelId);
        var teamSanitized = SanitizePathSegment(cref.TeamId);

        // Legacy v1 path used "channels/" (not "teams/") and a flat
        // "chat.message" event-type folder regardless of created/updated/
        // deleted. Match that exactly.
        var path = $"channels/{teamSanitized}/{cidSanitized}/chat.message/{ts}-{safeId}.txt";

        // Derive v1-equivalent fields.
        var payload = envelope.Payload as ChannelMessagePayload;
        var messageId = cref.MessageId ?? string.Empty;
        var channelThreadId = cref.ChannelId;
        var chatThreadId = string.IsNullOrEmpty(messageId)
            ? channelThreadId
            : $"{channelThreadId};messageid={messageId}";
        var convRefId = envelope.ConversationReferenceId ?? chatThreadId;
        var senderId = payload?.Sender?.AadId ?? string.Empty;
        var senderName = payload?.Sender?.DisplayName ?? string.Empty;
        var text = payload?.Text ?? string.Empty;
        var html = payload?.Html;
        var timestampUtc = payload?.TimestampUtc ?? envelope.Ts ?? string.Empty;
        var fromBot = payload?.FromBot ?? false;
        var v1InnerEventType = envelope.EventType switch
        {
            AlfredEventTypes.ChannelMessageUpdated => "chat_updated",
            AlfredEventTypes.ChannelMessageDeleted => "chat_deleted",
            _ => "chat_created",
        };

        // v1 envelope shape — flat top-level fields, flat payload.
        // Use sorted keys via a dictionary so JSON output is deterministic.
        var v1Envelope = new Dictionary<string, object?>
        {
            ["schema_version"] = "alfred-events-v1",
            ["event_type"] = "chat.message",
            ["event_id"] = envelope.EventId,
            ["ts"] = envelope.Ts,
            ["team_id"] = cref.TeamId,
            ["channel_id"] = cref.ChannelId,
            ["chat_thread_id"] = chatThreadId,
            ["channel_thread_id"] = channelThreadId,
            ["conversation_reference_id"] = convRefId,
            ["payload"] = new Dictionary<string, object?>
            {
                ["event_type"] = v1InnerEventType,
                ["chat_thread_id"] = chatThreadId,
                ["message_id"] = messageId,
                ["text"] = text,
                ["html"] = html,
                ["sender_id"] = senderId,
                ["sender_display_name"] = senderName,
                ["timestamp_utc"] = timestampUtc,
                ["conversation_reference_id"] = convRefId,
                ["attachments"] = Array.Empty<object>(),
                ["mentions"] = Array.Empty<object>(),
                ["from_bot"] = fromBot,
                ["conversation_kind"] = "channel",
                ["team_id"] = cref.TeamId,
                ["channel_id"] = cref.ChannelId,
                ["channel_thread_id"] = channelThreadId,
            },
        };

        // Human-readable header line (matches the pre-v2 format byte-for-byte).
        var senderTag = string.IsNullOrEmpty(senderName)
            ? (fromBot ? "Alfred (bot)" : "?")
            : (fromBot ? $"{senderName} (bot)" : senderName);
        var headerText = (text ?? string.Empty).Replace("\r\n", " ").Replace('\n', ' ').Replace('\r', ' ');
        var header = $"# [{timestampUtc}] {senderTag} (channel): {headerText}";

        var sb = new StringBuilder();
        sb.AppendLine(header);
        sb.AppendLine();
        sb.AppendLine("---ENVELOPE---");
        sb.Append(JsonSerializer.Serialize(v1Envelope, EnvelopeJsonOptions));
        return (path, sb.ToString());
    }

    /// <summary>
    /// Writes the full post-meeting Microsoft transcript to two
    /// well-known paths so an operator can grab the entire meeting in
    /// one shot:
    /// <list type="bullet">
    /// <item><c>_official-transcript.txt</c> — clean speaker-per-line
    /// plaintext, designed for a human to skim end-to-end.</item>
    /// <item><c>_official-transcript.vtt</c> — Microsoft's raw WebVTT,
    /// preserved verbatim for downstream tools that want cue timings.</item>
    /// </list>
    /// Both overwrite if a previous fetch already landed.
    /// </summary>
    public async Task ArchiveOfficialTranscriptAsync(
        string meetingId,
        string vttText,
        CancellationToken cancellationToken = default)
    {
        if (_container is null)
        {
            return;
        }
        if (string.IsNullOrWhiteSpace(meetingId) || string.IsNullOrWhiteSpace(vttText))
        {
            return;
        }

        try
        {
            var safeMeeting = SanitizePathSegment(meetingId);
            var basePath = $"meetings/{safeMeeting}/transcripts";

            var clean = RenderHumanReadableTranscript(meetingId, vttText);
            await UploadAsync($"{basePath}/official.txt", clean, "text/plain", cancellationToken);
            await UploadAsync($"{basePath}/official.vtt", vttText, "text/vtt", cancellationToken);

            _logger.LogInformation(
                "BlobEventArchive uploaded official transcript MeetingId={MeetingId} VttBytes={Bytes} CleanBytes={Clean} Path={Path}",
                meetingId, vttText.Length, clean.Length, basePath);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex,
                "BlobEventArchive official transcript write failed MeetingId={MeetingId}",
                meetingId);
        }
    }

    /// <summary>
    /// Re-renders Microsoft's WebVTT transcript into a flat human-
    /// friendly text file: one speaker per line, optional cue timestamp
    /// in <c>[hh:mm:ss]</c> prefix, no WebVTT timing headers or cue ids.
    /// Designed for an exec or engineer to read the entire meeting top
    /// to bottom without parsing.
    /// </summary>
    private static string RenderHumanReadableTranscript(string chatThreadId, string vtt)
    {
        var sb = new StringBuilder();
        sb.Append("# Meeting transcript — ").AppendLine(DateTimeOffset.UtcNow.ToString("yyyy-MM-dd HH:mm:ss 'UTC'", CultureInfo.InvariantCulture));
        sb.Append("# chat_thread_id: ").AppendLine(chatThreadId);
        sb.AppendLine("# Source: Microsoft Teams 'Record and Transcribe' (post-meeting fetch via Graph)");
        sb.AppendLine();

        var lines = vtt.Replace("\r\n", "\n").Split('\n');
        string? pendingStart = null;
        var bodyBuilder = new StringBuilder();
        void Flush(StringBuilder dest)
        {
            if (bodyBuilder.Length == 0) return;
            var body = bodyBuilder.ToString().Trim();
            if (body.Length == 0) { bodyBuilder.Clear(); return; }
            var prefix = pendingStart is null ? string.Empty : $"[{pendingStart}] ";
            // Each VTT cue body is "<v Speaker Name>Text</v>"; pull the speaker
            // out of the <v ...> tag if present, render as "Speaker: Text".
            string speaker = string.Empty;
            string text = body;
            var open = body.IndexOf("<v ", StringComparison.Ordinal);
            if (open >= 0)
            {
                var close = body.IndexOf('>', open);
                var endTag = body.IndexOf("</v>", StringComparison.Ordinal);
                if (close > open && endTag > close)
                {
                    speaker = body.Substring(open + 3, close - (open + 3)).Trim();
                    text = body.Substring(close + 1, endTag - (close + 1)).Trim();
                }
            }
            if (!string.IsNullOrWhiteSpace(speaker))
            {
                dest.Append(prefix).Append(speaker).Append(": ").AppendLine(text);
            }
            else
            {
                dest.Append(prefix).AppendLine(text);
            }
            bodyBuilder.Clear();
        }

        for (int i = 0; i < lines.Length; i++)
        {
            var line = lines[i].Trim();
            if (line.Length == 0)
            {
                Flush(sb);
                pendingStart = null;
                continue;
            }
            // Match WebVTT timing line: "hh:mm:ss.fff --> hh:mm:ss.fff"
            var arrow = line.IndexOf(" --> ", StringComparison.Ordinal);
            if (arrow > 0 && line.Length > arrow + 5)
            {
                var startRaw = line.Substring(0, arrow);
                var dot = startRaw.IndexOf('.');
                pendingStart = dot > 0 ? startRaw.Substring(0, dot) : startRaw;
                continue;
            }
            if (line.StartsWith("WEBVTT", StringComparison.Ordinal)) continue;
            if (line.StartsWith("NOTE", StringComparison.Ordinal)) continue;
            bodyBuilder.AppendLine(line);
        }
        Flush(sb);

        return sb.ToString();
    }

    /// <summary>
    /// Maps every v2 <c>event_type</c> to its logical category folder.
    /// One category aggregates all the underlying event-type variants
    /// that represent the same data type (e.g. <c>channel.message.created
    /// / updated / deleted</c> all land in <c>messages/</c>). The
    /// precise event type stays on the envelope JSON so any consumer
    /// that needs to distinguish reads it from there.
    /// </summary>
    private static readonly Dictionary<string, string> EventTypeToCategory =
        new(StringComparer.Ordinal)
        {
            // Channel scope
            { AlfredEventTypes.ChannelMessageCreated,     "messages" },
            { AlfredEventTypes.ChannelMessageUpdated,     "messages" },
            { AlfredEventTypes.ChannelMessageDeleted,     "messages" },
            { AlfredEventTypes.ChannelAttached,           "lifecycle" },
            { AlfredEventTypes.ChannelDetached,           "lifecycle" },

            // Meeting scope
            { AlfredEventTypes.MeetingChatCreated,        "messages" },
            { AlfredEventTypes.MeetingChatUpdated,        "messages" },
            { AlfredEventTypes.MeetingChatDeleted,        "messages" },
            { AlfredEventTypes.MeetingTranscriptPartial,  "live_transcript" },
            { AlfredEventTypes.MeetingTranscriptFinal,    "live_transcript" },
            { AlfredEventTypes.MeetingTranscriptOfficial, "transcripts" },
            { AlfredEventTypes.MeetingCreated,            "lifecycle" },
            { AlfredEventTypes.MeetingEnded,              "lifecycle" },
            { AlfredEventTypes.MeetingLinked,             "lifecycle" },
            { AlfredEventTypes.MeetingCallJoined,         "lifecycle" },
            { AlfredEventTypes.MeetingCallLeft,           "lifecycle" },
        };

    private static string CategoryFor(string? eventType)
    {
        if (eventType is not null && EventTypeToCategory.TryGetValue(eventType, out var cat))
        {
            return cat;
        }
        // Unknown event_type — fall back to a sanitized version of the
        // event_type itself so the envelope still lands somewhere
        // greppable instead of getting silently merged into a default.
        return SanitizePathSegment(eventType ?? "event");
    }

    internal static string BuildEnvelopePath(AlfredEventEnvelope envelope)
    {
        // envelope.Ts is an ISO 8601 string. Compact it into a sortable
        // filename segment; if it isn't parseable for any reason, fall
        // back to "now" so we still write _something_ rather than fail.
        var ts = DateTimeOffset.TryParse(envelope.Ts, CultureInfo.InvariantCulture,
            DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
            out var parsed)
            ? parsed.ToString("yyyyMMddTHHmmssfffZ", CultureInfo.InvariantCulture)
            : DateTimeOffset.UtcNow.ToString("yyyyMMddTHHmmssfffZ", CultureInfo.InvariantCulture);
        var safeId = SanitizePathSegment(envelope.EventId ?? Guid.NewGuid().ToString("N"));
        var category = CategoryFor(envelope.EventType);

        if (envelope.ChannelRef is { } cr)
        {
            return $"teams/{SanitizePathSegment(cr.TeamId)}/channels/{SanitizePathSegment(cr.ChannelId)}/{category}/{ts}-{safeId}.json";
        }
        if (envelope.MeetingRef is { } mr)
        {
            return $"meetings/{SanitizePathSegment(mr.MeetingId)}/{category}/{ts}-{safeId}.json";
        }
        return $"events/{category}/{ts}-{safeId}.json";
    }

    /// <summary>
    /// Sanitize a Teams id (which can contain <c>:</c>, <c>@</c>,
    /// <c>;</c>, <c>%</c>) into a single blob-path segment. Mirrors what
    /// <c>MeetingAuditLogger</c> does locally so a blob path is easy to
    /// correlate with the per-thread NDJSON file on disk.
    /// </summary>
    private static string SanitizePathSegment(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return "_";
        var replaced = UnsafePathChars.Replace(raw, "_");
        return replaced.Length > 200 ? replaced.Substring(0, 200) : replaced;
    }

    private async Task UploadAsync(
        string path,
        string content,
        string contentType,
        CancellationToken cancellationToken)
    {
        var blob = _container!.GetBlobClient(path);
        var bytes = Encoding.UTF8.GetBytes(content);
        using var stream = new MemoryStream(bytes);
        await blob.UploadAsync(stream,
            new BlobUploadOptions
            {
                HttpHeaders = new BlobHttpHeaders { ContentType = contentType },
            },
            cancellationToken);
    }
}
