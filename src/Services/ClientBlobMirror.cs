using System.Collections.Concurrent;
using System.Text;
using System.Text.Json;
using Azure.Storage.Blobs;
using Azure.Storage.Blobs.Models;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Uploads a copy of each routed envelope into a client-owned Azure
/// Blob container (the route's <c>storage_container_url</c>, a full
/// container URL carrying a SAS with create/write). Paths match the
/// central archive's canonical layout exactly
/// (<see cref="BlobEventArchive.BuildEnvelopePath"/>), so a client's
/// pull-based consumer (e.g. <c>server_v2.py</c>) can point at their
/// own container with zero path changes.
///
/// <para>
/// Fire-and-forget like the central archive: failures are logged and
/// swallowed, never blocking the dispatch path or the central write.
/// </para>
/// </summary>
public sealed class ClientBlobMirror
{
    private readonly ConcurrentDictionary<string, BlobContainerClient> _containers = new(StringComparer.Ordinal);
    private readonly ILogger<ClientBlobMirror> _logger;

    public ClientBlobMirror(ILogger<ClientBlobMirror> logger)
    {
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
    }

    public async Task MirrorAsync(
        ClientRouteRecord route,
        AlfredEventEnvelope envelope,
        CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(route.StorageContainerUrl)) return;

        try
        {
            var container = _containers.GetOrAdd(
                route.StorageContainerUrl!,
                static url => new BlobContainerClient(new Uri(url)));

            var path = BlobEventArchive.BuildEnvelopePath(envelope);
            var json = JsonSerializer.Serialize(envelope, BlobEventArchive.EnvelopeJsonOptions);
            var bytes = Encoding.UTF8.GetBytes(json);
            using var stream = new MemoryStream(bytes);
            await container.GetBlobClient(path).UploadAsync(
                stream,
                new BlobUploadOptions
                {
                    HttpHeaders = new BlobHttpHeaders { ContentType = "application/json" },
                },
                cancellationToken);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex,
                "Client blob mirror failed email={Email} event={EventType} ({EventId}); central archive is unaffected.",
                route.Email, envelope.EventType, envelope.EventId);
        }
    }
}
