using System.Collections.Concurrent;
using System.Text.Json;
using System.Text.Json.Serialization;
using NJ = Newtonsoft.Json;

namespace TeamsMediaBot.Services;

/// <summary>
/// Persistent record of a Teams channel that Alfred is attached to.
///
/// Channel attachment is the channel-level analog of "the bot is in this
/// meeting": once attached, Alfred listens to all messages in the channel
/// and is allowed to post back, no per-meeting handshake required. The
/// canonical thread id used by the rest of the system is
/// <see cref="ConversationThreadId"/> (Teams channel conversation id).
/// </summary>
public sealed record ChannelAttachmentRecord
{
    [JsonPropertyName("team_id")]
    public required string TeamId { get; init; }

    [JsonPropertyName("channel_id")]
    public required string ChannelId { get; init; }

    [JsonPropertyName("conversation_thread_id")]
    public string? ConversationThreadId { get; init; }

    [JsonPropertyName("team_display_name")]
    public string? TeamDisplayName { get; init; }

    [JsonPropertyName("channel_display_name")]
    public string? ChannelDisplayName { get; init; }

    [JsonPropertyName("service_url")]
    public string? ServiceUrl { get; init; }

    [JsonPropertyName("tenant_id")]
    public string? TenantId { get; init; }

    [JsonPropertyName("attached_at_utc")]
    public required DateTimeOffset AttachedAtUtc { get; init; }

    [JsonPropertyName("source")]
    public string? Source { get; init; }

    [JsonPropertyName("subscription_id")]
    public string? SubscriptionId { get; init; }

    [JsonPropertyName("subscription_resource")]
    public string? SubscriptionResource { get; init; }

    [JsonPropertyName("subscription_expires_at_utc")]
    public DateTimeOffset? SubscriptionExpiresAtUtc { get; init; }

    /// <summary>
    /// Downstream HTTP consumers that receive every event for this
    /// channel. Each consumer is one team's backend; the bot fans out
    /// the same versioned envelope to each. Empty means events for this
    /// channel are dropped after raw audit.
    /// </summary>
    [JsonPropertyName("consumers")]
    public IReadOnlyList<ConsumerConfig> Consumers { get; init; } = Array.Empty<ConsumerConfig>();

    /// <summary>
    /// True once the bootstrap-default consumer has been applied to
    /// this attachment. Latches forever — operators who delete
    /// <c>legacy-default</c> stay deleted across restarts.
    /// </summary>
    [JsonPropertyName("legacy_seeded")]
    public bool LegacySeeded { get; init; }

    public static string BuildKey(string teamId, string channelId) =>
        $"{teamId}|{channelId}";

    public string Key => BuildKey(TeamId, ChannelId);
}

/// <summary>
/// One HTTP destination registered against a channel attachment. The bot
/// POSTs every <see cref="Models.AlfredEventEnvelope"/> for that channel
/// here, optionally filtered by <see cref="EventKinds"/>. Each consumer
/// is a separate team's backend.
/// </summary>
public sealed record ConsumerConfig
{
    /// <summary>
    /// Stable name used as a dedupe key on PUT/DELETE. Required so
    /// operators can address one consumer in a list without relying on
    /// ordinal position.
    /// </summary>
    [JsonPropertyName("name")] [NJ.JsonProperty("name")]
    public required string Name { get; init; }

    /// <summary>Absolute HTTPS URL to POST envelopes to.</summary>
    [JsonPropertyName("url")] [NJ.JsonProperty("url")]
    public required string Url { get; init; }

    /// <summary>
    /// Event-type filter. <c>["*"]</c> (the default) accepts every event.
    /// Otherwise, only envelopes whose <see cref="Models.AlfredEventEnvelope.EventType"/>
    /// matches one of these values are forwarded.
    /// </summary>
    [JsonPropertyName("event_kinds")] [NJ.JsonProperty("event_kinds")]
    public IReadOnlyList<string> EventKinds { get; init; } = new[] { "*" };

    /// <summary>
    /// Optional headers added to every outbound POST. Use for upstream
    /// auth tokens, tenant tags, etc. Internal VPN deployment, no auth
    /// is required by default.
    /// </summary>
    [JsonPropertyName("headers")] [NJ.JsonProperty("headers")]
    public IReadOnlyDictionary<string, string>? Headers { get; init; }

    /// <summary>When false, the consumer is registered but skipped at dispatch time.</summary>
    [JsonPropertyName("enabled")] [NJ.JsonProperty("enabled")]
    public bool Enabled { get; init; } = true;
}

/// <summary>
/// JSON-file-backed store of channel attachments. Survives bot restarts so
/// channel attachment is genuinely persistent — once a channel is attached
/// it stays attached until explicitly detached.
///
/// <para>
/// Writes are serialized through a single mutex and use atomic
/// write-temp-then-rename so a crash mid-write can never produce a
/// half-written state file.
/// </para>
/// </summary>
public sealed class ChannelAttachmentStore
{
    private static readonly JsonSerializerOptions SerializerOptions = new(JsonSerializerDefaults.Web)
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly string _filePath;
    private readonly ILogger<ChannelAttachmentStore> _logger;
    private readonly SemaphoreSlim _mutex = new(1, 1);
    private readonly ConcurrentDictionary<string, ChannelAttachmentRecord> _byKey = new(StringComparer.Ordinal);

    public ChannelAttachmentStore(
        ChannelAttachmentStoreOptions options,
        ILogger<ChannelAttachmentStore> logger)
    {
        ArgumentNullException.ThrowIfNull(options);
        if (string.IsNullOrWhiteSpace(options.FilePath))
        {
            throw new InvalidOperationException(
                "ChannelAttachmentStoreOptions.FilePath must be set so channel attachments survive bot restarts.");
        }

        _filePath = options.FilePath;
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
    }

    public string FilePath => _filePath;

    public async Task LoadAsync(CancellationToken cancellationToken = default)
    {
        await _mutex.WaitAsync(cancellationToken);
        try
        {
            _byKey.Clear();
            if (!File.Exists(_filePath))
            {
                _logger.LogInformation(
                    "Channel attachment file {FilePath} does not exist yet; starting with no attachments.",
                    _filePath);
                return;
            }

            await using var stream = File.OpenRead(_filePath);
            var records = await JsonSerializer.DeserializeAsync<List<ChannelAttachmentRecord>>(
                stream,
                SerializerOptions,
                cancellationToken);

            if (records is null)
            {
                return;
            }

            foreach (var record in records)
            {
                _byKey[record.Key] = record;
            }

            _logger.LogInformation(
                "Loaded {Count} channel attachments from {FilePath}",
                _byKey.Count,
                _filePath);
        }
        finally
        {
            _mutex.Release();
        }
    }

    public IReadOnlyList<ChannelAttachmentRecord> List() => _byKey.Values.ToList();

    public ChannelAttachmentRecord? Get(string teamId, string channelId)
    {
        if (string.IsNullOrWhiteSpace(teamId) || string.IsNullOrWhiteSpace(channelId))
        {
            return null;
        }

        return _byKey.TryGetValue(ChannelAttachmentRecord.BuildKey(teamId, channelId), out var record)
            ? record
            : null;
    }

    public ChannelAttachmentRecord? GetByConversationThreadId(string conversationThreadId)
    {
        if (string.IsNullOrWhiteSpace(conversationThreadId))
        {
            return null;
        }

        foreach (var record in _byKey.Values)
        {
            if (string.Equals(record.ConversationThreadId, conversationThreadId, StringComparison.Ordinal))
            {
                return record;
            }
        }

        return null;
    }

    public bool IsAttached(string teamId, string channelId) =>
        Get(teamId, channelId) is not null;

    public async Task UpsertAsync(ChannelAttachmentRecord record, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(record);

        await _mutex.WaitAsync(cancellationToken);
        try
        {
            _byKey[record.Key] = record;
            await PersistLockedAsync(cancellationToken);
        }
        finally
        {
            _mutex.Release();
        }
    }

    /// <summary>
    /// Replaces the consumer list on an existing attachment. Returns
    /// false if no attachment exists for that <c>(teamId, channelId)</c>.
    /// </summary>
    public async Task<bool> SetConsumersAsync(
        string teamId,
        string channelId,
        IReadOnlyList<ConsumerConfig> consumers,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(consumers);
        if (string.IsNullOrWhiteSpace(teamId) || string.IsNullOrWhiteSpace(channelId))
        {
            return false;
        }

        await _mutex.WaitAsync(cancellationToken);
        try
        {
            var key = ChannelAttachmentRecord.BuildKey(teamId, channelId);
            if (!_byKey.TryGetValue(key, out var existing))
            {
                return false;
            }

            _byKey[key] = existing with { Consumers = NormalizeConsumers(consumers) };
            await PersistLockedAsync(cancellationToken);
            return true;
        }
        finally
        {
            _mutex.Release();
        }
    }

    /// <summary>
    /// Inserts or replaces a single consumer by <see cref="ConsumerConfig.Name"/>.
    /// Returns false if no attachment exists.
    /// </summary>
    public async Task<bool> UpsertConsumerAsync(
        string teamId,
        string channelId,
        ConsumerConfig consumer,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(consumer);
        if (string.IsNullOrWhiteSpace(teamId) || string.IsNullOrWhiteSpace(channelId))
        {
            return false;
        }

        await _mutex.WaitAsync(cancellationToken);
        try
        {
            var key = ChannelAttachmentRecord.BuildKey(teamId, channelId);
            if (!_byKey.TryGetValue(key, out var existing))
            {
                return false;
            }

            var normalized = NormalizeConsumer(consumer);
            var updated = existing.Consumers
                .Where(c => !string.Equals(c.Name, normalized.Name, StringComparison.Ordinal))
                .Append(normalized)
                .ToList();

            _byKey[key] = existing with { Consumers = updated };
            await PersistLockedAsync(cancellationToken);
            return true;
        }
        finally
        {
            _mutex.Release();
        }
    }

    /// <summary>
    /// Removes a single consumer by name. Returns false if no attachment
    /// exists or if no consumer by that name was registered.
    /// </summary>
    public async Task<bool> RemoveConsumerAsync(
        string teamId,
        string channelId,
        string consumerName,
        CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(teamId)
            || string.IsNullOrWhiteSpace(channelId)
            || string.IsNullOrWhiteSpace(consumerName))
        {
            return false;
        }

        await _mutex.WaitAsync(cancellationToken);
        try
        {
            var key = ChannelAttachmentRecord.BuildKey(teamId, channelId);
            if (!_byKey.TryGetValue(key, out var existing))
            {
                return false;
            }

            var filtered = existing.Consumers
                .Where(c => !string.Equals(c.Name, consumerName, StringComparison.Ordinal))
                .ToList();

            if (filtered.Count == existing.Consumers.Count)
            {
                return false;
            }

            _byKey[key] = existing with { Consumers = filtered };
            await PersistLockedAsync(cancellationToken);
            return true;
        }
        finally
        {
            _mutex.Release();
        }
    }

    private static IReadOnlyList<ConsumerConfig> NormalizeConsumers(IReadOnlyList<ConsumerConfig> consumers)
    {
        var seen = new HashSet<string>(StringComparer.Ordinal);
        var result = new List<ConsumerConfig>(consumers.Count);
        foreach (var c in consumers)
        {
            var n = NormalizeConsumer(c);
            if (seen.Add(n.Name))
            {
                result.Add(n);
            }
        }
        return result;
    }

    private static ConsumerConfig NormalizeConsumer(ConsumerConfig consumer)
    {
        if (string.IsNullOrWhiteSpace(consumer.Name))
        {
            throw new InvalidOperationException("ConsumerConfig.Name is required.");
        }
        if (string.IsNullOrWhiteSpace(consumer.Url))
        {
            throw new InvalidOperationException("ConsumerConfig.Url is required.");
        }
        if (!Uri.TryCreate(consumer.Url, UriKind.Absolute, out _))
        {
            throw new InvalidOperationException(
                $"ConsumerConfig.Url must be an absolute URL. Got: '{consumer.Url}'.");
        }

        return consumer with
        {
            Name = consumer.Name.Trim(),
            Url = consumer.Url.Trim(),
            EventKinds = consumer.EventKinds is { Count: > 0 } kinds
                ? kinds.Select(k => k.Trim()).Where(k => k.Length > 0).ToList()
                : new[] { "*" },
        };
    }

    public async Task<bool> RemoveAsync(string teamId, string channelId, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(teamId) || string.IsNullOrWhiteSpace(channelId))
        {
            return false;
        }

        await _mutex.WaitAsync(cancellationToken);
        try
        {
            var key = ChannelAttachmentRecord.BuildKey(teamId, channelId);
            if (!_byKey.TryRemove(key, out _))
            {
                return false;
            }

            await PersistLockedAsync(cancellationToken);
            return true;
        }
        finally
        {
            _mutex.Release();
        }
    }

    private async Task PersistLockedAsync(CancellationToken cancellationToken)
    {
        var directory = Path.GetDirectoryName(_filePath);
        if (!string.IsNullOrWhiteSpace(directory) && !Directory.Exists(directory))
        {
            Directory.CreateDirectory(directory);
        }

        var tempPath = _filePath + ".tmp";
        await using (var stream = File.Create(tempPath))
        {
            await JsonSerializer.SerializeAsync(
                stream,
                _byKey.Values.OrderBy(r => r.Key, StringComparer.Ordinal).ToList(),
                SerializerOptions,
                cancellationToken);
        }

        File.Move(tempPath, _filePath, overwrite: true);
    }
}

/// <summary>
/// Configuration for <see cref="ChannelAttachmentStore"/>.
/// </summary>
public sealed class ChannelAttachmentStoreOptions
{
    /// <summary>Absolute path to the JSON state file.</summary>
    public required string FilePath { get; init; }
}
