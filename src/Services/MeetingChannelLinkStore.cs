using System.Collections.Concurrent;
using System.Text.Json;
using System.Text.Json.Serialization;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// One persistent link from a meeting chat thread (or any non-channel
/// chat where Alfred has been added) to its "owning" channel. Lets an
/// operator schedule a private Teams meeting, add Alfred to the
/// meeting chat, and tell the bot "this meeting belongs to the
/// alfred_test channel". From that moment on, every envelope from that
/// chat_thread_id is stamped with the linked team/channel ids so its
/// blob archive entries land under the channel's folder and the sink's
/// session_channel_links table sees the join automatically.
/// </summary>
public sealed record MeetingChannelLinkRecord
{
    [JsonPropertyName("chat_thread_id")]
    public required string ChatThreadId { get; init; }

    [JsonPropertyName("team_id")]
    public required string TeamId { get; init; }

    [JsonPropertyName("channel_id")]
    public required string ChannelId { get; init; }

    [JsonPropertyName("channel_thread_id")]
    public string? ChannelThreadId { get; init; }

    [JsonPropertyName("team_display_name")]
    public string? TeamDisplayName { get; init; }

    [JsonPropertyName("channel_display_name")]
    public string? ChannelDisplayName { get; init; }

    [JsonPropertyName("linked_at_utc")]
    public DateTimeOffset LinkedAtUtc { get; init; } = DateTimeOffset.UtcNow;

    [JsonPropertyName("source")]
    public string? Source { get; init; }
}

public sealed class MeetingChannelLinkStoreOptions
{
    public required string FilePath { get; init; }
}

/// <summary>
/// File-backed, in-memory <see cref="MeetingChannelLinkRecord"/>
/// registry keyed by <c>chat_thread_id</c>. Persistence layout matches
/// <see cref="ChannelAttachmentStore"/> so ops can hand-edit the JSON
/// in a pinch. Implements <see cref="IHostedService"/> so the singleton
/// reads its disk state once at startup before any envelope publish
/// can look it up.
/// </summary>
public sealed class MeetingChannelLinkStore : IHostedService
{
    private static readonly JsonSerializerOptions SerializerOptions = new(JsonSerializerDefaults.Web)
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly string _filePath;
    private readonly ILogger<MeetingChannelLinkStore> _logger;
    private readonly SemaphoreSlim _mutex = new(1, 1);
    private readonly ConcurrentDictionary<string, MeetingChannelLinkRecord> _byThreadId = new(StringComparer.Ordinal);

    public MeetingChannelLinkStore(
        MeetingChannelLinkStoreOptions options,
        ILogger<MeetingChannelLinkStore> logger)
    {
        ArgumentNullException.ThrowIfNull(options);
        if (string.IsNullOrWhiteSpace(options.FilePath))
        {
            throw new InvalidOperationException(
                "MeetingChannelLinkStoreOptions.FilePath must be set so links survive bot restarts.");
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
            _byThreadId.Clear();
            if (!File.Exists(_filePath))
            {
                _logger.LogInformation(
                    "MeetingChannelLinkStore file {FilePath} does not exist yet; starting empty.",
                    _filePath);
                return;
            }
            await using var stream = File.OpenRead(_filePath);
            var records = await JsonSerializer.DeserializeAsync<List<MeetingChannelLinkRecord>>(
                stream, SerializerOptions, cancellationToken);
            if (records is null) return;
            foreach (var r in records)
            {
                _byThreadId[r.ChatThreadId] = r;
            }
            _logger.LogInformation(
                "Loaded {Count} meeting-channel links from {FilePath}",
                _byThreadId.Count, _filePath);
        }
        finally
        {
            _mutex.Release();
        }
    }

    public MeetingChannelLinkRecord? Get(string chatThreadId)
    {
        if (string.IsNullOrWhiteSpace(chatThreadId)) return null;
        // Strip Bot Framework's ;messageid= suffix so a reply-thread chat id
        // still hits the parent meeting's link.
        var key = chatThreadId;
        var semi = key.IndexOf(';');
        if (semi >= 0) key = key.Substring(0, semi);
        return _byThreadId.TryGetValue(key, out var record) ? record : null;
    }

    /// <summary>
    /// Returns a <see cref="ChannelLink"/> for the given Graph
    /// <c>onlineMeeting</c> id. Also checks the <c>meet-{id}</c> prefix
    /// used for short-URL joins. Returns null when no link is found.
    /// Full rewrite to key by meeting_id is Task 7.
    /// </summary>
    public ChannelLink? GetChannelLink(string meetingId)
    {
        if (string.IsNullOrWhiteSpace(meetingId)) return null;
        var record = Get(meetingId) ?? Get($"meet-{meetingId}");
        if (record is null) return null;
        return new ChannelLink
        {
            TeamId = record.TeamId,
            TeamDisplayName = record.TeamDisplayName,
            ChannelId = record.ChannelId,
            ChannelDisplayName = record.ChannelDisplayName,
            ThreadId = record.ChannelThreadId,
            LinkedAtUtc = record.LinkedAtUtc.ToString("O"),
            LinkedSource = record.Source ?? "unknown",
        };
    }

    public IReadOnlyList<MeetingChannelLinkRecord> List() => _byThreadId.Values.ToList();

    public async Task UpsertAsync(MeetingChannelLinkRecord record, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(record);
        await _mutex.WaitAsync(cancellationToken);
        try
        {
            _byThreadId[record.ChatThreadId] = record;
            await WriteToDiskAsync(cancellationToken);
        }
        finally
        {
            _mutex.Release();
        }
    }

    public async Task<bool> RemoveAsync(string chatThreadId, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(chatThreadId)) return false;
        await _mutex.WaitAsync(cancellationToken);
        try
        {
            var removed = _byThreadId.TryRemove(chatThreadId, out _);
            if (removed) await WriteToDiskAsync(cancellationToken);
            return removed;
        }
        finally
        {
            _mutex.Release();
        }
    }

    private async Task WriteToDiskAsync(CancellationToken cancellationToken)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(_filePath)!);
        var tmp = _filePath + ".tmp";
        await using (var stream = File.Create(tmp))
        {
            await JsonSerializer.SerializeAsync(stream, _byThreadId.Values.ToList(), SerializerOptions, cancellationToken);
        }
        File.Move(tmp, _filePath, overwrite: true);
    }
}
