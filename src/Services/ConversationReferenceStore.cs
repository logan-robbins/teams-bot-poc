using System.Collections.Concurrent;
using Microsoft.Bot.Schema;
using NJ = Newtonsoft.Json;

namespace TeamsMediaBot.Services;

/// <summary>
/// Process-local cache of Bot Framework <see cref="ConversationReference"/>s
/// keyed by chat thread id.
///
/// <para>
/// Bot Framework proactive messaging (the only supported 2026 path for an
/// application-permission bot to post into a meeting chat) requires a
/// captured <see cref="ConversationReference"/>. We grab the reference from
/// the first activity the Bot Framework adapter delivers for a given chat
/// and keep it here until the meeting ends.
/// </para>
///
/// <para>
/// Implementations should survive a process restart so <c>/api/send-chat</c>
/// keeps working after a deploy without each meeting chat having to emit a
/// fresh activity first.
/// </para>
/// </summary>
public interface IConversationReferenceStore
{
    /// <summary>Insert or replace the reference for a chat thread id.</summary>
    void Put(string chatThreadId, ConversationReference reference);

    /// <summary>Lookup by chat thread id, or null if unknown.</summary>
    ConversationReference? Get(string chatThreadId);

    /// <summary>Remove a chat thread id's reference. Returns true if a record existed.</summary>
    bool Remove(string chatThreadId);

    /// <summary>Snapshot of every chat thread id known to the store.</summary>
    IReadOnlyCollection<string> KnownChatThreadIds { get; }
}

/// <summary>
/// Configuration for <see cref="FileBackedConversationReferenceStore"/>.
/// </summary>
public sealed class ConversationReferenceStoreOptions
{
    /// <summary>Absolute path to the JSON state file.</summary>
    public required string FilePath { get; init; }
}

/// <summary>
/// File-backed, in-memory <see cref="IConversationReferenceStore"/>. Layout
/// matches <see cref="ChannelAttachmentStore"/> and
/// <see cref="MeetingChannelLinkStore"/>: a single mutex-serialized
/// write-temp-then-atomic-rename so a crash mid-write can never produce a
/// half-written state file.
///
/// <para>
/// Writes flush to disk immediately (no debounce) — the bot rarely sees
/// more than a handful of fresh chat references per second and the on-disk
/// payload is small, so eagerly persisting matches the rest of the state
/// stores and keeps the deploy/restart story trivial: whatever
/// <see cref="Put(string, ConversationReference)"/> returned has already
/// been persisted by the time the call completes.
/// </para>
///
/// <para>
/// Serialization uses Newtonsoft.Json because Bot Framework's
/// <see cref="ConversationReference"/> is decorated with Newtonsoft
/// attributes; System.Text.Json would silently drop or rename properties.
/// </para>
///
/// <para>
/// Implements <see cref="IHostedService"/> so the singleton reads its disk
/// state once during application start before any
/// <c>/api/send-chat</c> request can look it up.
/// </para>
/// </summary>
public sealed class FileBackedConversationReferenceStore : IConversationReferenceStore, IHostedService
{
    private static readonly NJ.JsonSerializerSettings SerializerSettings = new()
    {
        Formatting = NJ.Formatting.Indented,
        NullValueHandling = NJ.NullValueHandling.Ignore,
    };

    private readonly string _filePath;
    private readonly ILogger<FileBackedConversationReferenceStore> _logger;
    private readonly SemaphoreSlim _mutex = new(1, 1);
    private readonly ConcurrentDictionary<string, ConversationReference> _refs =
        new(StringComparer.Ordinal);

    public FileBackedConversationReferenceStore(
        ConversationReferenceStoreOptions options,
        ILogger<FileBackedConversationReferenceStore> logger)
    {
        ArgumentNullException.ThrowIfNull(options);
        if (string.IsNullOrWhiteSpace(options.FilePath))
        {
            throw new InvalidOperationException(
                "ConversationReferenceStoreOptions.FilePath must be set so references survive bot restarts.");
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
            _refs.Clear();
            if (!File.Exists(_filePath))
            {
                _logger.LogInformation(
                    "ConversationReference store file {FilePath} does not exist yet; starting empty.",
                    _filePath);
                return;
            }

            string json;
            try
            {
                json = await File.ReadAllTextAsync(_filePath, cancellationToken);
            }
            catch (IOException ex)
            {
                _logger.LogWarning(ex,
                    "Failed to read ConversationReference store {FilePath}; starting empty.",
                    _filePath);
                return;
            }

            Dictionary<string, ConversationReference>? loaded;
            try
            {
                loaded = NJ.JsonConvert.DeserializeObject<Dictionary<string, ConversationReference>>(
                    json, SerializerSettings);
            }
            catch (NJ.JsonException ex)
            {
                _logger.LogWarning(ex,
                    "ConversationReference store {FilePath} is corrupt or unreadable; starting empty.",
                    _filePath);
                return;
            }

            if (loaded is null)
            {
                return;
            }

            foreach (var (key, value) in loaded)
            {
                if (string.IsNullOrWhiteSpace(key) || value is null) continue;
                _refs[key] = value;
            }

            _logger.LogInformation(
                "Loaded {Count} conversation references from {FilePath}",
                _refs.Count, _filePath);
        }
        finally
        {
            _mutex.Release();
        }
    }

    public void Put(string chatThreadId, ConversationReference reference)
    {
        if (string.IsNullOrWhiteSpace(chatThreadId))
        {
            throw new ArgumentException("chatThreadId must be non-empty", nameof(chatThreadId));
        }
        ArgumentNullException.ThrowIfNull(reference);

        _refs[chatThreadId] = reference;

        // Match ChannelAttachmentStore / MeetingChannelLinkStore: persist
        // synchronously on every mutation so a restart immediately after a
        // capture loses nothing. The interface is sync (Put), so we block
        // the mutex acquisition + write. Burst rate is low (1 per fresh
        // chat thread per process lifetime), so eager-write is fine.
        try
        {
            _mutex.Wait();
            try
            {
                PersistLocked();
            }
            finally
            {
                _mutex.Release();
            }
        }
        catch (Exception ex)
        {
            // Persistence failure must not break the in-memory put — the
            // next restart will simply re-capture from the next fresh
            // activity, which is the pre-file-backed behavior.
            _logger.LogWarning(ex,
                "Failed to persist ConversationReference store {FilePath} after Put({ChatThreadId}); in-memory state retained.",
                _filePath, chatThreadId);
        }
    }

    public ConversationReference? Get(string chatThreadId) =>
        string.IsNullOrWhiteSpace(chatThreadId)
            ? null
            : _refs.TryGetValue(chatThreadId, out var reference) ? reference : null;

    public bool Remove(string chatThreadId)
    {
        if (string.IsNullOrWhiteSpace(chatThreadId))
        {
            return false;
        }

        var removed = _refs.TryRemove(chatThreadId, out _);
        if (!removed)
        {
            return false;
        }

        try
        {
            _mutex.Wait();
            try
            {
                PersistLocked();
            }
            finally
            {
                _mutex.Release();
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex,
                "Failed to persist ConversationReference store {FilePath} after Remove({ChatThreadId}); in-memory state retained.",
                _filePath, chatThreadId);
        }
        return true;
    }

    public IReadOnlyCollection<string> KnownChatThreadIds => _refs.Keys.ToList();

    /// <summary>
    /// Atomic temp-write + rename. Caller must hold <see cref="_mutex"/>.
    /// </summary>
    private void PersistLocked()
    {
        var directory = Path.GetDirectoryName(_filePath);
        if (!string.IsNullOrWhiteSpace(directory) && !Directory.Exists(directory))
        {
            Directory.CreateDirectory(directory);
        }

        // Snapshot under the lock so we never serialize a half-mutated dict.
        var snapshot = _refs.ToDictionary(
            kv => kv.Key,
            kv => kv.Value,
            StringComparer.Ordinal);

        var tempPath = _filePath + ".tmp";
        var json = NJ.JsonConvert.SerializeObject(snapshot, SerializerSettings);
        File.WriteAllText(tempPath, json);
        File.Move(tempPath, _filePath, overwrite: true);
    }
}
