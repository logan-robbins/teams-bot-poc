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
/// are also persisted as individual .txt blobs so downstream consumers
/// don't have to ingest the POST fan-out to retain history.
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

    public bool IsConfigured =>
        !string.IsNullOrWhiteSpace(ConnectionString) ||
        !string.IsNullOrWhiteSpace(AccountUrl);
}

/// <summary>
/// Writes Alfred event envelopes and post-meeting official transcripts
/// to Azure Blob Storage as <c>.txt</c> files keyed by team / channel
/// (or chat thread) and event kind. Sits parallel to the
/// <see cref="EventFanoutDispatcher"/>'s HTTP fan-out path: every event
/// the dispatcher publishes to a Python sink is also persisted here.
/// </summary>
/// <remarks>
/// Path layout (all lowercase, slash-prefixed virtual folders):
///   channels/{teamId}/{sanitizedChannelId}/{eventKind}/{utcTs}-{eventId}.txt
///   meetings/{sanitizedChatThreadId}/{eventKind}/{utcTs}-{eventId}.txt
///   meetings/{sanitizedChatThreadId}/_official-transcript.txt
///
/// Auth: prefers <see cref="BlobArchiveConfiguration.ConnectionString"/>
/// when set (account-key path, current sandbox state) and falls back to
/// <see cref="DefaultAzureCredential"/> against
/// <see cref="BlobArchiveConfiguration.AccountUrl"/> so we can swap to
/// managed identity later without code changes.
/// </remarks>
public sealed class BlobEventArchive
{
    private static readonly JsonSerializerOptions EnvelopeJsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull,
        WriteIndented = true,
    };

    private static readonly Regex UnsafePathChars = new(@"[^a-zA-Z0-9\-_.]", RegexOptions.Compiled);

    private readonly BlobContainerClient? _container;
    private readonly ILogger<BlobEventArchive> _logger;

    public BlobEventArchive(BlobArchiveConfiguration config, ILogger<BlobEventArchive> logger)
    {
        _logger = logger;
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
            "BlobEventArchive ready: container={Container} endpoint={Endpoint}",
            config.ContainerName, _container.Uri);
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
                var body = BuildHumanReadableBody(envelope);
                await UploadAsync(path, body, "text/plain", cancellationToken);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex,
                    "BlobEventArchive envelope write failed EventType={EventType} EventId={EventId}",
                    envelope.EventType, envelope.EventId);
            }
        }, cancellationToken);
    }

    /// <summary>
    /// Renders an envelope into a two-part blob body: a small human-
    /// readable preamble (timestamp + sender + summary) followed by the
    /// full machine-parseable JSON envelope under <c>---ENVELOPE---</c>.
    /// </summary>
    private static string BuildHumanReadableBody(AlfredEventEnvelope envelope)
    {
        var sb = new StringBuilder();
        sb.Append("# ").AppendLine(BuildHumanSummary(envelope));
        sb.AppendLine();
        sb.AppendLine("---ENVELOPE---");
        sb.Append(JsonSerializer.Serialize(envelope, EnvelopeJsonOptions));
        return sb.ToString();
    }

    private static string BuildHumanSummary(AlfredEventEnvelope envelope)
    {
        var ts = envelope.Ts ?? string.Empty;
        return envelope.EventType switch
        {
            AlfredEventTypes.ChatMessage => SummarizeChat(envelope, ts),
            AlfredEventTypes.TranscriptPartial => SummarizeTranscript(envelope, ts, "partial"),
            AlfredEventTypes.TranscriptFinal => SummarizeTranscript(envelope, ts, "final"),
            AlfredEventTypes.TranscriptOfficial => $"[{ts}] official transcript fetched (event_id={envelope.EventId})",
            AlfredEventTypes.ChannelAttached => $"[{ts}] channel attached (team={envelope.TeamId} channel={envelope.ChannelId})",
            AlfredEventTypes.ChannelDetached => $"[{ts}] channel detached (team={envelope.TeamId} channel={envelope.ChannelId})",
            AlfredEventTypes.SessionLinked => $"[{ts}] session linked (thread={envelope.ChatThreadId} team={envelope.TeamId} channel={envelope.ChannelId})",
            _ => $"[{ts}] {envelope.EventType} (event_id={envelope.EventId})",
        };
    }

    private static string SummarizeChat(AlfredEventEnvelope envelope, string ts)
    {
        if (envelope.Payload is ChatEventPayload p)
        {
            var sender = string.IsNullOrWhiteSpace(p.SenderDisplayName)
                ? (p.SenderId ?? "?")
                : p.SenderDisplayName!;
            var botTag = p.FromBot ? " (bot)" : string.Empty;
            var kind = string.IsNullOrWhiteSpace(p.ConversationKind) ? "chat" : p.ConversationKind!;
            var text = (p.Text ?? string.Empty).Replace('\n', ' ');
            if (text.Length > 400) text = text.Substring(0, 400) + "…";
            return $"[{ts}] {sender}{botTag} ({kind}): {text}";
        }
        return $"[{ts}] chat.message (event_id={envelope.EventId})";
    }

    private static string SummarizeTranscript(AlfredEventEnvelope envelope, string ts, string kind)
    {
        if (envelope.Payload is TranscriptEvent te)
        {
            var speaker = te.SpeakerId ?? "?";
            var text = (te.Text ?? string.Empty).Replace('\n', ' ');
            if (text.Length > 400) text = text.Substring(0, 400) + "…";
            return $"[{ts}] {speaker} ({kind}): {text}";
        }
        return $"[{ts}] transcript.{kind} (event_id={envelope.EventId})";
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
        string chatThreadId,
        string vttText,
        CancellationToken cancellationToken = default)
    {
        if (_container is null)
        {
            return;
        }
        if (string.IsNullOrWhiteSpace(chatThreadId) || string.IsNullOrWhiteSpace(vttText))
        {
            return;
        }

        try
        {
            var safeThread = SanitizePathSegment(chatThreadId);
            var basePath = $"meetings/{safeThread}";

            var clean = RenderHumanReadableTranscript(chatThreadId, vttText);
            await UploadAsync($"{basePath}/_official-transcript.txt", clean, "text/plain", cancellationToken);
            await UploadAsync($"{basePath}/_official-transcript.vtt", vttText, "text/vtt", cancellationToken);

            _logger.LogInformation(
                "BlobEventArchive uploaded official transcript ChatThreadId={ChatThreadId} VttBytes={Bytes} CleanBytes={Clean} Path={Path}",
                chatThreadId, vttText.Length, clean.Length, basePath);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex,
                "BlobEventArchive official transcript write failed ChatThreadId={ChatThreadId}",
                chatThreadId);
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

    private static string BuildEnvelopePath(AlfredEventEnvelope envelope)
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

        // Teams emits meeting lifecycle events (call started, meeting
        // ended, recording exported, transcript ready) into a channel's
        // chat stream as system-event chat messages whose body.content
        // is JSON with scopeId + callId. They arrive at the bot as
        // chat.message envelopes and would otherwise drown the
        // chat.message/ folder. Route them to a separate
        // system.meeting_lifecycle/ folder so the chat.message/ folder
        // contains only real human + bot chat.
        var folderKind = LooksLikeTeamsMeetingSystemMessage(envelope)
            ? "system.meeting_lifecycle"
            : envelope.EventType ?? "event";
        var safeKind = SanitizePathSegment(folderKind);

        if (!string.IsNullOrWhiteSpace(envelope.TeamId) &&
            !string.IsNullOrWhiteSpace(envelope.ChannelId))
        {
            return $"channels/{SanitizePathSegment(envelope.TeamId!)}/{SanitizePathSegment(envelope.ChannelId!)}/{safeKind}/{ts}-{safeId}.txt";
        }
        var threadKey = envelope.ChatThreadId ?? "unknown-thread";
        return $"meetings/{SanitizePathSegment(threadKey)}/{safeKind}/{ts}-{safeId}.txt";
    }

    /// <summary>
    /// True iff this is a chat.message envelope whose <c>text</c> payload
    /// is a Teams meeting system-event JSON blob (scopeId + callId).
    /// These are noise in the chat.message/ folder; we route them to a
    /// sibling system.meeting_lifecycle/ folder so the chat.message/
    /// folder stays clean for human readers.
    /// </summary>
    private static bool LooksLikeTeamsMeetingSystemMessage(AlfredEventEnvelope envelope)
    {
        if (!string.Equals(envelope.EventType, AlfredEventTypes.ChatMessage, StringComparison.Ordinal))
        {
            return false;
        }
        if (envelope.Payload is not ChatEventPayload p) return false;
        var text = p.Text;
        if (string.IsNullOrWhiteSpace(text)) return false;
        if (!text.TrimStart().StartsWith("{", StringComparison.Ordinal)) return false;
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
