using System.Collections.Concurrent;
using System.Net;
using System.Text.Json;

namespace TeamsMediaBot.Services;

/// <summary>
/// Typed, cached accessor for the Graph metadata Alfred needs to
/// stamp human-readable names + canonical ids onto every published
/// event:
///   - team display name (<c>GET /teams/{id}</c>)
///   - channel display name + membership type
///     (<c>GET /teams/{tid}/channels/{cid}</c>)
///   - meeting chat metadata (<c>GET /chats/{chat_id}</c>) — used to
///     bridge from the bot's known <c>meeting_chat_thread_id</c> to
///     the <c>joinWebUrl</c>, which then resolves to the canonical
///     <c>onlineMeeting</c> id.
///   - online meeting metadata (<c>GET /users/{organizer}/onlineMeetings/{id}</c>)
///   - channel message metadata (<c>GET /teams/{tid}/channels/{cid}/messages/{id}</c>)
///     for thread root previews.
///
/// All methods return <c>null</c> on 404 and re-throw other Graph
/// errors. Results are cached in-process with a short TTL — display
/// names change rarely, but we never want to serve a stale id.
/// </summary>
public sealed class GraphMetadataResolver
{
    private readonly GraphApiClient _graph;
    private readonly ILogger<GraphMetadataResolver> _logger;
    private readonly TimeSpan _ttl = TimeSpan.FromMinutes(15);

    private readonly ConcurrentDictionary<string, CacheEntry<GraphTeamMeta>> _teams = new();
    private readonly ConcurrentDictionary<string, CacheEntry<GraphChannelMeta>> _channels = new();
    private readonly ConcurrentDictionary<string, CacheEntry<GraphChatMeta>> _chats = new();
    private readonly ConcurrentDictionary<string, CacheEntry<GraphOnlineMeetingMeta>> _meetings = new();
    private readonly ConcurrentDictionary<string, CacheEntry<GraphChannelMessage>> _channelMessages = new();
    private readonly ConcurrentDictionary<string, CacheEntry<string>> _chatToMeetingId = new();
    private readonly ConcurrentDictionary<string, CacheEntry<GraphUserMeta>> _users = new();

    public GraphMetadataResolver(GraphApiClient graph, ILogger<GraphMetadataResolver> logger)
    {
        _graph = graph ?? throw new ArgumentNullException(nameof(graph));
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
    }

    public async Task<GraphTeamMeta?> GetTeamAsync(string teamId, CancellationToken ct = default)
    {
        if (TryGetCached(_teams, teamId, out var cached)) return cached;

        var meta = await FetchAsync(
            $"teams/{Uri.EscapeDataString(teamId)}?$select=id,displayName,description",
            root => new GraphTeamMeta
            {
                Id = teamId,
                DisplayName = TryGetString(root, "displayName"),
                Description = TryGetString(root, "description"),
            },
            ct);

        return Cache(_teams, teamId, meta);
    }

    public async Task<GraphChannelMeta?> GetChannelAsync(
        string teamId, string channelId, CancellationToken ct = default)
    {
        var key = $"{teamId}|{channelId}";
        if (TryGetCached(_channels, key, out var cached)) return cached;

        var meta = await FetchAsync(
            $"teams/{Uri.EscapeDataString(teamId)}/channels/{Uri.EscapeDataString(channelId)}?$select=id,displayName,description,membershipType",
            root => new GraphChannelMeta
            {
                Id = channelId,
                TeamId = teamId,
                DisplayName = TryGetString(root, "displayName"),
                Description = TryGetString(root, "description"),
                MembershipType = TryGetString(root, "membershipType"),
            },
            ct);

        return Cache(_channels, key, meta);
    }

    /// <summary>
    /// Resolve a meeting chat thread id (<c>19:meeting_xxx@thread.v2</c>)
    /// to its chat metadata, which includes the joinWebUrl and chat
    /// type. Use the joinWebUrl to then resolve the canonical
    /// <c>onlineMeeting</c> id via <see cref="GetOnlineMeetingByJoinUrlAsync"/>.
    /// </summary>
    public async Task<GraphChatMeta?> GetChatAsync(string chatId, CancellationToken ct = default)
    {
        if (TryGetCached(_chats, chatId, out var cached)) return cached;

        var meta = await FetchAsync(
            $"chats/{Uri.EscapeDataString(chatId)}",
            root =>
            {
                string? joinWebUrl = null;
                string? organizerOid = null;
                if (root.TryGetProperty("onlineMeetingInfo", out var omi) &&
                    omi.ValueKind != JsonValueKind.Null)
                {
                    if (omi.TryGetProperty("joinWebUrl", out var jwu))
                    {
                        joinWebUrl = jwu.GetString();
                    }
                    if (omi.TryGetProperty("organizer", out var org) &&
                        org.ValueKind == JsonValueKind.Object)
                    {
                        organizerOid = TryGetString(org, "id");
                    }
                }
                return new GraphChatMeta
                {
                    Id = chatId,
                    ChatType = TryGetString(root, "chatType"),
                    Topic = TryGetString(root, "topic"),
                    JoinWebUrl = joinWebUrl,
                    OrganizerAadId = organizerOid,
                };
            },
            ct);

        return Cache(_chats, chatId, meta);
    }

    /// <summary>
    /// Bridge a meeting chat thread id (<c>19:meeting_xxx@thread.v2</c>) to
    /// the canonical Graph <c>onlineMeeting.id</c> (URL-safe base64). Per
    /// MS Graph v1.0 (Microsoft.Graph 5.92), <c>chatInfo.threadId</c> and
    /// <c>onlineMeeting.id</c> are distinct keys; the contract requires
    /// <c>meeting_id</c> on every published envelope to be the latter.
    /// Two-hop resolution: <c>GET /chats/{id}</c> → <c>joinWebUrl</c> +
    /// organizer → <c>GET /users/{org}/onlineMeetings?$filter=joinWebUrl
    /// eq '...'</c>. Cached.
    /// </summary>
    public async Task<string?> ResolveCanonicalMeetingIdAsync(
        string chatThreadId, CancellationToken ct = default)
    {
        if (string.IsNullOrWhiteSpace(chatThreadId)) return null;
        if (TryGetCached(_chatToMeetingId, chatThreadId, out var cached)) return cached;

        var chat = await GetChatAsync(chatThreadId, ct);
        if (chat?.JoinWebUrl is null || chat.OrganizerAadId is null)
        {
            return null;
        }

        var meeting = await GetOnlineMeetingByJoinUrlAsync(chat.OrganizerAadId, chat.JoinWebUrl, ct);
        if (string.IsNullOrWhiteSpace(meeting?.Id))
        {
            return null;
        }

        return Cache(_chatToMeetingId, chatThreadId, meeting!.Id);
    }

    /// <summary>
    /// Resolve a meeting by its canonical Graph <c>onlineMeeting</c> id,
    /// scoped under the organizer's user resource.
    /// </summary>
    public async Task<GraphOnlineMeetingMeta?> GetOnlineMeetingAsync(
        string organizerOid, string meetingId, CancellationToken ct = default)
    {
        var key = $"{organizerOid}|{meetingId}";
        if (TryGetCached(_meetings, key, out var cached)) return cached;

        var meta = await FetchAsync(
            $"users/{Uri.EscapeDataString(organizerOid)}/onlineMeetings/{Uri.EscapeDataString(meetingId)}",
            ParseOnlineMeeting,
            ct);

        return Cache(_meetings, key, meta);
    }

    /// <summary>
    /// Resolve a meeting by its joinWebUrl. This is the recommended
    /// path when you only know the meeting chat thread id (which gives
    /// you joinWebUrl via <see cref="GetChatAsync"/>).
    /// </summary>
    public async Task<GraphOnlineMeetingMeta?> GetOnlineMeetingByJoinUrlAsync(
        string organizerOid, string joinWebUrl, CancellationToken ct = default)
    {
        var filter = Uri.EscapeDataString($"joinWebUrl eq '{joinWebUrl}'");

        var meta = await FetchAsync(
            $"users/{Uri.EscapeDataString(organizerOid)}/onlineMeetings?$filter={filter}",
            root =>
            {
                if (root.TryGetProperty("value", out var arr) &&
                    arr.ValueKind == JsonValueKind.Array &&
                    arr.GetArrayLength() > 0)
                {
                    return ParseOnlineMeeting(arr[0]);
                }
                return null!;
            },
            default);

        if (meta is null) return null;

        var key = $"{organizerOid}|{meta.Id}";
        return Cache(_meetings, key, meta);
    }

    public async Task<GraphChannelMessage?> GetChannelMessageAsync(
        string teamId, string channelId, string messageId, CancellationToken ct = default)
    {
        var key = $"{teamId}|{channelId}|{messageId}";
        if (TryGetCached(_channelMessages, key, out var cached)) return cached;

        var meta = await FetchAsync(
            $"teams/{Uri.EscapeDataString(teamId)}/channels/{Uri.EscapeDataString(channelId)}/messages/{Uri.EscapeDataString(messageId)}",
            root =>
            {
                string? bodyContent = null;
                if (root.TryGetProperty("body", out var body))
                {
                    bodyContent = TryGetString(body, "content");
                }
                string? fromDisplay = null;
                if (root.TryGetProperty("from", out var from) &&
                    from.ValueKind != JsonValueKind.Null &&
                    from.TryGetProperty("user", out var user) &&
                    user.ValueKind != JsonValueKind.Null)
                {
                    fromDisplay = TryGetString(user, "displayName");
                }
                return new GraphChannelMessage
                {
                    Id = messageId,
                    BodyContent = bodyContent,
                    FromDisplayName = fromDisplay,
                    CreatedDateTime = TryGetString(root, "createdDateTime"),
                };
            },
            ct);

        return Cache(_channelMessages, key, meta);
    }

    /// <summary>
    /// Resolve an AAD user object id to mail + userPrincipalName for
    /// email-based client routing. Requires the tenant-wide
    /// <c>User.ReadBasic.All</c> application permission (README §5.2);
    /// with RSC-only grants this returns null and callers fall back to
    /// Bot Framework <c>TeamsInfo</c> identity.
    /// </summary>
    public async Task<GraphUserMeta?> GetUserAsync(string userId, CancellationToken ct = default)
    {
        if (string.IsNullOrWhiteSpace(userId)) return null;
        if (TryGetCached(_users, userId, out var cached)) return cached;

        var meta = await FetchAsync(
            $"users/{Uri.EscapeDataString(userId)}?$select=id,displayName,mail,userPrincipalName",
            root => new GraphUserMeta
            {
                Id = userId,
                DisplayName = TryGetString(root, "displayName"),
                Mail = TryGetString(root, "mail"),
                UserPrincipalName = TryGetString(root, "userPrincipalName"),
            },
            ct);

        return Cache(_users, userId, meta);
    }

    /// <summary>
    /// Force a re-fetch on next read. Call when a Graph notification
    /// indicates the resource changed.
    /// </summary>
    public void Invalidate()
    {
        _teams.Clear();
        _channels.Clear();
        _chats.Clear();
        _meetings.Clear();
        _channelMessages.Clear();
        _chatToMeetingId.Clear();
        _users.Clear();
    }

    private async Task<T?> FetchAsync<T>(string resource, Func<JsonElement, T> parse, CancellationToken ct)
        where T : class
    {
        try
        {
            using var doc = await _graph.GetResourceAsync(resource, ct);
            return parse(doc.RootElement);
        }
        catch (GraphApiException ex) when (ex.StatusCode == HttpStatusCode.NotFound)
        {
            return null;
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Graph metadata fetch failed for {Resource}", resource);
            return null;
        }
    }

    private bool TryGetCached<T>(ConcurrentDictionary<string, CacheEntry<T>> cache, string key, out T? value)
        where T : class
    {
        if (cache.TryGetValue(key, out var entry) && DateTimeOffset.UtcNow - entry.FetchedAt < _ttl)
        {
            value = entry.Value;
            return true;
        }
        value = null;
        return false;
    }

    private T? Cache<T>(ConcurrentDictionary<string, CacheEntry<T>> cache, string key, T? value)
        where T : class
    {
        if (value is not null)
        {
            cache[key] = new CacheEntry<T>(value, DateTimeOffset.UtcNow);
        }
        return value;
    }

    private static GraphOnlineMeetingMeta ParseOnlineMeeting(JsonElement m)
    {
        string? organizerAad = null;
        string? organizerName = null;
        if (m.TryGetProperty("participants", out var participants) &&
            participants.ValueKind != JsonValueKind.Null &&
            participants.TryGetProperty("organizer", out var org) &&
            org.ValueKind != JsonValueKind.Null &&
            org.TryGetProperty("identity", out var identity) &&
            identity.ValueKind != JsonValueKind.Null &&
            identity.TryGetProperty("user", out var user) &&
            user.ValueKind != JsonValueKind.Null)
        {
            organizerAad = TryGetString(user, "id");
            organizerName = TryGetString(user, "displayName");
        }

        return new GraphOnlineMeetingMeta
        {
            Id = TryGetString(m, "id") ?? string.Empty,
            Subject = TryGetString(m, "subject"),
            JoinWebUrl = TryGetString(m, "joinWebUrl"),
            ScheduledStartUtc = TryGetString(m, "startDateTime"),
            ScheduledEndUtc = TryGetString(m, "endDateTime"),
            OrganizerAadId = organizerAad,
            OrganizerDisplayName = organizerName,
        };
    }

    private static string? TryGetString(JsonElement el, string name) =>
        el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString()
            : null;

    private sealed record CacheEntry<T>(T Value, DateTimeOffset FetchedAt);
}

public sealed record GraphTeamMeta
{
    public required string Id { get; init; }
    public string? DisplayName { get; init; }
    public string? Description { get; init; }
}

public sealed record GraphChannelMeta
{
    public required string Id { get; init; }
    public required string TeamId { get; init; }
    public string? DisplayName { get; init; }
    public string? Description { get; init; }
    public string? MembershipType { get; init; }
}

public sealed record GraphChatMeta
{
    public required string Id { get; init; }
    public string? ChatType { get; init; }
    public string? Topic { get; init; }
    public string? JoinWebUrl { get; init; }
    public string? OrganizerAadId { get; init; }
}

public sealed record GraphOnlineMeetingMeta
{
    public required string Id { get; init; }
    public string? Subject { get; init; }
    public string? JoinWebUrl { get; init; }
    public string? ScheduledStartUtc { get; init; }
    public string? ScheduledEndUtc { get; init; }
    public string? OrganizerAadId { get; init; }
    public string? OrganizerDisplayName { get; init; }
}

public sealed record GraphChannelMessage
{
    public required string Id { get; init; }
    public string? BodyContent { get; init; }
    public string? FromDisplayName { get; init; }
    public string? CreatedDateTime { get; init; }
}

public sealed record GraphUserMeta
{
    public required string Id { get; init; }
    public string? DisplayName { get; init; }
    public string? Mail { get; init; }
    public string? UserPrincipalName { get; init; }
}
