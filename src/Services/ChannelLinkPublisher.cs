using System.Collections.Concurrent;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Publishes <c>chat_thread_id → (team_id, channel_id, channel_thread_id)</c>
/// links to the Python sink via <c>POST /session/link</c>. The sink
/// persists the binding and backfills every prior <c>meeting_events</c>
/// and <c>raw_ingest_events</c> row for that thread, so analytics can
/// later filter every transcript / chat / system event by
/// <c>channel_id</c> alone — even for events that landed before the
/// link was known.
///
/// <para>
/// This client is the bot's "we just learned this meeting belongs to
/// channel X" notifier. It de-duplicates per <c>(chat_thread_id, team_id,
/// channel_id)</c> tuple in process so we don't hammer the sink with
/// the same link on every chat activity.
/// </para>
/// </summary>
public sealed class ChannelLinkPublisher
{
    private static readonly JsonSerializerOptions SerializerOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly HttpClient _httpClient;
    private readonly ILogger<ChannelLinkPublisher> _logger;
    private readonly string? _endpoint;
    private readonly ConcurrentDictionary<string, byte> _published = new(StringComparer.Ordinal);

    public ChannelLinkPublisher(
        HttpClient httpClient,
        TranscriptSinkConfiguration config,
        ILogger<ChannelLinkPublisher> logger)
    {
        _httpClient = httpClient;
        _logger = logger;

        // Derive the sink base URL from the chat endpoint and append /session/link.
        // We deliberately do NOT add a separate config field — the sink lives
        // at one origin and exposes both /chat and /session/link side-by-side.
        if (!string.IsNullOrWhiteSpace(config?.ChatEndpoint))
        {
            var chatUrl = config!.ChatEndpoint!.TrimEnd('/');
            var lastSlash = chatUrl.LastIndexOf('/');
            var baseUrl = lastSlash > 0 ? chatUrl[..lastSlash] : chatUrl;
            _endpoint = $"{baseUrl}/session/link";
        }
    }

    public async Task PublishLinkAsync(
        string chatThreadId,
        string teamId,
        string channelId,
        string? channelThreadId = null,
        string? source = null,
        CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(_endpoint))
        {
            return;
        }
        if (string.IsNullOrWhiteSpace(chatThreadId)
            || string.IsNullOrWhiteSpace(teamId)
            || string.IsNullOrWhiteSpace(channelId))
        {
            return;
        }

        var dedupeKey = string.Join("|", chatThreadId, teamId, channelId, channelThreadId ?? "");
        if (!_published.TryAdd(dedupeKey, 1))
        {
            return;
        }

        var payload = new ChannelLinkPayload
        {
            ChatThreadId = chatThreadId,
            TeamId = teamId,
            ChannelId = channelId,
            ChannelThreadId = channelThreadId,
            Source = source,
        };

        try
        {
            using var response = await _httpClient.PostAsJsonAsync(
                _endpoint,
                payload,
                SerializerOptions,
                cancellationToken);

            if (!response.IsSuccessStatusCode)
            {
                // Allow a retry next time we see the link.
                _published.TryRemove(dedupeKey, out _);
                var body = await response.Content.ReadAsStringAsync(cancellationToken);
                _logger.LogWarning(
                    "Sink /session/link returned {Status}: {Body}",
                    (int)response.StatusCode,
                    body.Length > 200 ? body[..200] : body);
            }
            else
            {
                _logger.LogInformation(
                    "Published session-channel link: chat_thread_id={ChatThreadId} team={TeamId} channel={ChannelId}",
                    chatThreadId,
                    teamId,
                    channelId);
            }
        }
        catch (Exception ex)
        {
            _published.TryRemove(dedupeKey, out _);
            _logger.LogWarning(
                ex,
                "Failed to POST session-channel link to {Endpoint} for {ChatThreadId}",
                _endpoint,
                chatThreadId);
        }
    }
}

internal sealed record ChannelLinkPayload
{
    [JsonPropertyName("chat_thread_id")] public required string ChatThreadId { get; init; }
    [JsonPropertyName("team_id")] public required string TeamId { get; init; }
    [JsonPropertyName("channel_id")] public required string ChannelId { get; init; }
    [JsonPropertyName("channel_thread_id")] public string? ChannelThreadId { get; init; }
    [JsonPropertyName("source")] public string? Source { get; init; }
}
