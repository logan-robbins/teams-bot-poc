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
                var json = JsonSerializer.Serialize(envelope, EnvelopeJsonOptions);
                await UploadAsync(path, json, "application/json", cancellationToken);
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
    /// Writes the full post-meeting Microsoft transcript to a
    /// well-known path so an operator can download the entire meeting in
    /// one shot. Overwrites if a previous fetch already landed.
    /// </summary>
    public async Task ArchiveOfficialTranscriptAsync(
        string chatThreadId,
        string transcriptText,
        CancellationToken cancellationToken = default)
    {
        if (_container is null)
        {
            return;
        }
        if (string.IsNullOrWhiteSpace(chatThreadId) || string.IsNullOrWhiteSpace(transcriptText))
        {
            return;
        }

        try
        {
            var safeThread = SanitizePathSegment(chatThreadId);
            var path = $"meetings/{safeThread}/_official-transcript.txt";
            await UploadAsync(path, transcriptText, "text/plain", cancellationToken);
            _logger.LogInformation(
                "BlobEventArchive uploaded official transcript ChatThreadId={ChatThreadId} Bytes={Bytes} Path={Path}",
                chatThreadId, transcriptText.Length, path);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex,
                "BlobEventArchive official transcript write failed ChatThreadId={ChatThreadId}",
                chatThreadId);
        }
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
        var safeKind = SanitizePathSegment(envelope.EventType ?? "event");
        if (!string.IsNullOrWhiteSpace(envelope.TeamId) &&
            !string.IsNullOrWhiteSpace(envelope.ChannelId))
        {
            return $"channels/{SanitizePathSegment(envelope.TeamId!)}/{SanitizePathSegment(envelope.ChannelId!)}/{safeKind}/{ts}-{safeId}.txt";
        }
        var threadKey = envelope.ChatThreadId ?? "unknown-thread";
        return $"meetings/{SanitizePathSegment(threadKey)}/{safeKind}/{ts}-{safeId}.txt";
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
