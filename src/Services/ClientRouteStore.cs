using System.Collections.Concurrent;
using System.Text.Json;
using System.Text.Json.Serialization;
using NJ = Newtonsoft.Json;

namespace TeamsMediaBot.Services;

/// <summary>
/// One client-owned Alfred destination, keyed by email. Email is the
/// public client_id (PLAN.md): a client registers email + sink URL
/// (+ optional storage container) once, and the bot routes every
/// meeting that person installs/organizes to their endpoints without
/// the client ever knowing Teams ids.
/// </summary>
public sealed record ClientRouteRecord
{
    /// <summary>Lowercased email; primary key.</summary>
    [JsonPropertyName("email")] [NJ.JsonProperty("email")]
    public required string Email { get; init; }

    /// <summary>Absolute HTTPS URL envelopes are POSTed to (usually /v2/events).</summary>
    [JsonPropertyName("sink_url")] [NJ.JsonProperty("sink_url")]
    public required string SinkUrl { get; init; }

    /// <summary>Event-type filter; <c>["*"]</c> (default) accepts every event.</summary>
    [JsonPropertyName("event_kinds")] [NJ.JsonProperty("event_kinds")]
    public IReadOnlyList<string> EventKinds { get; init; } = new[] { "*" };

    /// <summary>Optional headers added to every outbound POST (client auth).</summary>
    [JsonPropertyName("headers")] [NJ.JsonProperty("headers")]
    public IReadOnlyDictionary<string, string>? Headers { get; init; }

    /// <summary>
    /// Optional client-owned Azure Blob container URL (with SAS query
    /// string granting create/write). When set, every envelope routed
    /// to this client is also uploaded to that container at the same
    /// canonical path layout the central archive uses. The central
    /// archive write is unaffected.
    /// </summary>
    [JsonPropertyName("storage_container_url")] [NJ.JsonProperty("storage_container_url")]
    public string? StorageContainerUrl { get; init; }

    /// <summary>When false, the route is registered but never used.</summary>
    [JsonPropertyName("enabled")] [NJ.JsonProperty("enabled")]
    public bool Enabled { get; init; } = true;

    [JsonPropertyName("created_at_utc")] [NJ.JsonProperty("created_at_utc")]
    public DateTimeOffset CreatedAtUtc { get; init; } = DateTimeOffset.UtcNow;

    [JsonPropertyName("updated_at_utc")] [NJ.JsonProperty("updated_at_utc")]
    public DateTimeOffset UpdatedAtUtc { get; init; } = DateTimeOffset.UtcNow;

    public static string NormalizeEmail(string email) => email.Trim().ToLowerInvariant();
}

/// <summary>
/// Internal identity mapping: AAD object id → email. Teams activities
/// reliably expose AAD ids; email is what clients register. Cached
/// here so routing never re-resolves the same person twice.
/// </summary>
public sealed record ClientIdentityAliasRecord
{
    [JsonPropertyName("email")] [NJ.JsonProperty("email")]
    public required string Email { get; init; }

    [JsonPropertyName("tenant_id")] [NJ.JsonProperty("tenant_id")]
    public string? TenantId { get; init; }

    [JsonPropertyName("aad_object_id")] [NJ.JsonProperty("aad_object_id")]
    public required string AadObjectId { get; init; }

    /// <summary>graph_user_lookup | teams_activity | manual</summary>
    [JsonPropertyName("source")] [NJ.JsonProperty("source")]
    public string? Source { get; init; }

    [JsonPropertyName("created_at_utc")] [NJ.JsonProperty("created_at_utc")]
    public DateTimeOffset CreatedAtUtc { get; init; } = DateTimeOffset.UtcNow;

    [JsonPropertyName("updated_at_utc")] [NJ.JsonProperty("updated_at_utc")]
    public DateTimeOffset UpdatedAtUtc { get; init; } = DateTimeOffset.UtcNow;
}

/// <summary>
/// Sticky binding from a meeting chat thread to a client route. Live
/// transcript chunks carry only the meeting/chat id; this table means
/// they never re-resolve the person's identity per chunk.
/// </summary>
public sealed record MeetingRouteRecord
{
    /// <summary>19:meeting_…@thread.v2 — primary key.</summary>
    [JsonPropertyName("meeting_chat_thread_id")] [NJ.JsonProperty("meeting_chat_thread_id")]
    public required string MeetingChatThreadId { get; init; }

    /// <summary>Canonical meeting id from the envelope, when known.</summary>
    [JsonPropertyName("meeting_id")] [NJ.JsonProperty("meeting_id")]
    public string? MeetingId { get; init; }

    [JsonPropertyName("email")] [NJ.JsonProperty("email")]
    public required string Email { get; init; }

    /// <summary>installer | organizer | sender | manual</summary>
    [JsonPropertyName("source")] [NJ.JsonProperty("source")]
    public string? Source { get; init; }

    [JsonPropertyName("created_at_utc")] [NJ.JsonProperty("created_at_utc")]
    public DateTimeOffset CreatedAtUtc { get; init; } = DateTimeOffset.UtcNow;

    [JsonPropertyName("updated_at_utc")] [NJ.JsonProperty("updated_at_utc")]
    public DateTimeOffset UpdatedAtUtc { get; init; } = DateTimeOffset.UtcNow;
}

/// <summary>On-disk shape: the three routing tables in one JSON file.</summary>
public sealed record ClientRouteStoreState
{
    [JsonPropertyName("routes")]
    public List<ClientRouteRecord> Routes { get; init; } = new();

    [JsonPropertyName("aliases")]
    public List<ClientIdentityAliasRecord> Aliases { get; init; } = new();

    [JsonPropertyName("meeting_routes")]
    public List<MeetingRouteRecord> MeetingRoutes { get; init; } = new();
}

public sealed class ClientRouteStoreOptions
{
    public required string FilePath { get; init; }
}

/// <summary>
/// File-backed, in-memory registry for the email-based client routing
/// tables (client_routes, client_identity_aliases, meeting_routes).
/// Persistence layout matches <see cref="ChannelAttachmentStore"/> so
/// ops can hand-edit the JSON in a pinch. Implements
/// <see cref="IHostedService"/> so the singleton reads its disk state
/// once at startup before any envelope publish can look it up.
/// </summary>
public sealed class ClientRouteStore : IHostedService
{
    private static readonly JsonSerializerOptions SerializerOptions = new(JsonSerializerDefaults.Web)
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly string _filePath;
    private readonly ILogger<ClientRouteStore> _logger;
    private readonly SemaphoreSlim _mutex = new(1, 1);
    private readonly ConcurrentDictionary<string, ClientRouteRecord> _routesByEmail = new(StringComparer.Ordinal);
    private readonly ConcurrentDictionary<string, ClientIdentityAliasRecord> _aliasesByAadId = new(StringComparer.OrdinalIgnoreCase);
    private readonly ConcurrentDictionary<string, MeetingRouteRecord> _meetingRoutesByThreadId = new(StringComparer.Ordinal);

    public ClientRouteStore(ClientRouteStoreOptions options, ILogger<ClientRouteStore> logger)
    {
        ArgumentNullException.ThrowIfNull(options);
        if (string.IsNullOrWhiteSpace(options.FilePath))
        {
            throw new InvalidOperationException(
                "ClientRouteStoreOptions.FilePath must be set so client routes survive bot restarts.");
        }

        _filePath = options.FilePath;
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
    }

    public string FilePath => _filePath;

    public Task StartAsync(CancellationToken cancellationToken) => LoadAsync(cancellationToken);

    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;

    public async Task LoadAsync(CancellationToken cancellationToken = default)
    {
        await _mutex.WaitAsync(cancellationToken);
        try
        {
            _routesByEmail.Clear();
            _aliasesByAadId.Clear();
            _meetingRoutesByThreadId.Clear();
            if (!File.Exists(_filePath))
            {
                _logger.LogInformation(
                    "ClientRouteStore file {FilePath} does not exist yet; starting empty.", _filePath);
                return;
            }

            await using var stream = File.OpenRead(_filePath);
            var state = await JsonSerializer.DeserializeAsync<ClientRouteStoreState>(
                stream, SerializerOptions, cancellationToken);
            if (state is null) return;

            foreach (var r in state.Routes)
            {
                _routesByEmail[ClientRouteRecord.NormalizeEmail(r.Email)] = r;
            }
            foreach (var a in state.Aliases)
            {
                _aliasesByAadId[a.AadObjectId] = a;
            }
            foreach (var m in state.MeetingRoutes)
            {
                _meetingRoutesByThreadId[m.MeetingChatThreadId] = m;
            }

            _logger.LogInformation(
                "Loaded {Routes} client routes, {Aliases} identity aliases, {MeetingRoutes} meeting routes from {FilePath}",
                _routesByEmail.Count, _aliasesByAadId.Count, _meetingRoutesByThreadId.Count, _filePath);
        }
        finally
        {
            _mutex.Release();
        }
    }

    // ---- client_routes ---------------------------------------------------

    public IReadOnlyList<ClientRouteRecord> ListRoutes() => _routesByEmail.Values.ToList();

    public bool HasEnabledRoutes => _routesByEmail.Values.Any(r => r.Enabled);

    public ClientRouteRecord? GetRoute(string email) =>
        string.IsNullOrWhiteSpace(email)
            ? null
            : _routesByEmail.GetValueOrDefault(ClientRouteRecord.NormalizeEmail(email));

    public async Task UpsertRouteAsync(ClientRouteRecord record, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(record);
        await _mutex.WaitAsync(cancellationToken);
        try
        {
            var key = ClientRouteRecord.NormalizeEmail(record.Email);
            var existing = _routesByEmail.GetValueOrDefault(key);
            _routesByEmail[key] = record with
            {
                Email = key,
                CreatedAtUtc = existing?.CreatedAtUtc ?? record.CreatedAtUtc,
                UpdatedAtUtc = DateTimeOffset.UtcNow,
            };
            await PersistLockedAsync(cancellationToken);
        }
        finally
        {
            _mutex.Release();
        }
    }

    public async Task<bool> RemoveRouteAsync(string email, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(email)) return false;
        await _mutex.WaitAsync(cancellationToken);
        try
        {
            var removed = _routesByEmail.TryRemove(ClientRouteRecord.NormalizeEmail(email), out _);
            if (removed) await PersistLockedAsync(cancellationToken);
            return removed;
        }
        finally
        {
            _mutex.Release();
        }
    }

    // ---- client_identity_aliases ------------------------------------------

    public string? GetAliasEmail(string aadObjectId) =>
        string.IsNullOrWhiteSpace(aadObjectId)
            ? null
            : _aliasesByAadId.GetValueOrDefault(aadObjectId)?.Email;

    public async Task UpsertAliasAsync(ClientIdentityAliasRecord record, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(record);
        await _mutex.WaitAsync(cancellationToken);
        try
        {
            var existing = _aliasesByAadId.GetValueOrDefault(record.AadObjectId);
            _aliasesByAadId[record.AadObjectId] = record with
            {
                Email = ClientRouteRecord.NormalizeEmail(record.Email),
                CreatedAtUtc = existing?.CreatedAtUtc ?? record.CreatedAtUtc,
                UpdatedAtUtc = DateTimeOffset.UtcNow,
            };
            await PersistLockedAsync(cancellationToken);
        }
        finally
        {
            _mutex.Release();
        }
    }

    // ---- meeting_routes ----------------------------------------------------

    /// <summary>
    /// Strips Bot Framework's <c>;messageid=</c> suffix so a reply-thread
    /// chat id still hits the parent meeting's route (same normalization
    /// as <see cref="MeetingChannelLinkStore.Get"/>).
    /// </summary>
    private static string NormalizeThreadId(string chatThreadId)
    {
        var semi = chatThreadId.IndexOf(';');
        return semi >= 0 ? chatThreadId[..semi] : chatThreadId;
    }

    public MeetingRouteRecord? GetMeetingRoute(string meetingChatThreadId) =>
        string.IsNullOrWhiteSpace(meetingChatThreadId)
            ? null
            : _meetingRoutesByThreadId.GetValueOrDefault(NormalizeThreadId(meetingChatThreadId));

    public IReadOnlyList<MeetingRouteRecord> ListMeetingRoutes(string? email = null)
    {
        var all = _meetingRoutesByThreadId.Values;
        if (string.IsNullOrWhiteSpace(email)) return all.ToList();
        var key = ClientRouteRecord.NormalizeEmail(email);
        return all.Where(m => string.Equals(m.Email, key, StringComparison.Ordinal)).ToList();
    }

    public async Task UpsertMeetingRouteAsync(MeetingRouteRecord record, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(record);
        await _mutex.WaitAsync(cancellationToken);
        try
        {
            var key = NormalizeThreadId(record.MeetingChatThreadId);
            var existing = _meetingRoutesByThreadId.GetValueOrDefault(key);
            _meetingRoutesByThreadId[key] = record with
            {
                MeetingChatThreadId = key,
                Email = ClientRouteRecord.NormalizeEmail(record.Email),
                MeetingId = record.MeetingId ?? existing?.MeetingId,
                CreatedAtUtc = existing?.CreatedAtUtc ?? record.CreatedAtUtc,
                UpdatedAtUtc = DateTimeOffset.UtcNow,
            };
            await PersistLockedAsync(cancellationToken);
        }
        finally
        {
            _mutex.Release();
        }
    }

    /// <summary>
    /// Joins meeting_routes → client_routes: the enabled client route a
    /// meeting chat thread is bound to, or null (which means: fall back
    /// to the normal channel-consumer / bootstrap resolution).
    /// </summary>
    public ClientRouteRecord? RouteForMeeting(string? meetingChatThreadId)
    {
        if (string.IsNullOrWhiteSpace(meetingChatThreadId)) return null;
        var binding = GetMeetingRoute(meetingChatThreadId);
        if (binding is null) return null;
        var route = GetRoute(binding.Email);
        return route is { Enabled: true } ? route : null;
    }

    private async Task PersistLockedAsync(CancellationToken cancellationToken)
    {
        var directory = Path.GetDirectoryName(_filePath);
        if (!string.IsNullOrWhiteSpace(directory) && !Directory.Exists(directory))
        {
            Directory.CreateDirectory(directory);
        }

        var state = new ClientRouteStoreState
        {
            Routes = _routesByEmail.Values.OrderBy(r => r.Email, StringComparer.Ordinal).ToList(),
            Aliases = _aliasesByAadId.Values.OrderBy(a => a.AadObjectId, StringComparer.Ordinal).ToList(),
            MeetingRoutes = _meetingRoutesByThreadId.Values
                .OrderBy(m => m.MeetingChatThreadId, StringComparer.Ordinal).ToList(),
        };

        var tempPath = _filePath + ".tmp";
        await using (var stream = File.Create(tempPath))
        {
            await JsonSerializer.SerializeAsync(stream, state, SerializerOptions, cancellationToken);
        }

        File.Move(tempPath, _filePath, overwrite: true);
    }
}
