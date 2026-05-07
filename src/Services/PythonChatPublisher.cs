using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Forwards meeting-chat events to the Python sink's /chat endpoint.
///
/// Mirrors the shape of <see cref="PythonTranscriptPublisher"/> for /transcript.
/// The sink expects snake_case keys and a schema matching ChatMessageRequest
/// in python/transcript_sink.py.
/// </summary>
public sealed class PythonChatPublisher
{
    private static readonly JsonSerializerOptions SerializerOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly HttpClient _httpClient;
    private readonly ILogger<PythonChatPublisher> _logger;
    private readonly string _endpoint;
    private readonly MeetingAuditLogger? _auditLogger;

    public PythonChatPublisher(
        HttpClient httpClient,
        TranscriptSinkConfiguration config,
        ILogger<PythonChatPublisher> logger,
        MeetingAuditLogger? auditLogger = null)
    {
        _httpClient = httpClient ?? throw new ArgumentNullException(nameof(httpClient));
        ArgumentNullException.ThrowIfNull(config);
        if (string.IsNullOrWhiteSpace(config.ChatEndpoint))
        {
            throw new InvalidOperationException(
                "TranscriptSink.ChatEndpoint is required because inbound Teams chat is forwarded to the Python /chat endpoint.");
        }

        _endpoint = config.ChatEndpoint;
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
        _auditLogger = auditLogger;
    }

    public async Task PublishAsync(ChatEventPayload payload, CancellationToken cancellationToken = default)
    {
        try
        {
            if (_auditLogger is not null && !string.IsNullOrWhiteSpace(payload.ChatThreadId))
                _auditLogger.Append(payload.ChatThreadId, "chat", payload);

            using var response = await _httpClient.PostAsJsonAsync(
                _endpoint,
                payload,
                SerializerOptions,
                cancellationToken);

            if (!response.IsSuccessStatusCode)
            {
                var body = await response.Content.ReadAsStringAsync(cancellationToken);
                _logger.LogWarning(
                    "Chat sink returned {Status}: {Body}",
                    (int)response.StatusCode,
                    body.Length > 200 ? body[..200] : body);
            }
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to POST chat event {MessageId} to {Endpoint}",
                payload.MessageId, _endpoint);
        }
    }
}

/// <summary>
/// Wire shape sent to the Python sink's /chat endpoint.
/// Must stay in sync with ChatMessageRequest in python/transcript_sink.py.
///
/// <para>
/// <c>chat_thread_id</c> is the canonical session key. For meeting chats it is
/// <c>19:meeting_xxx@thread.v2</c>; for Teams channels it is
/// <c>19:{channel-id}@thread.tacv2</c> (or <c>@thread.skype</c> for legacy
/// channels). <c>conversation_kind</c> distinguishes them, and
/// <c>team_id</c>/<c>channel_id</c> are populated for channel activities.
/// </para>
/// </summary>
public sealed record ChatEventPayload
{
    [JsonPropertyName("event_type")] public string EventType { get; init; } = "chat_created";
    [JsonPropertyName("chat_thread_id")] public required string ChatThreadId { get; init; }
    [JsonPropertyName("message_id")] public required string MessageId { get; init; }
    public string? Text { get; init; }
    public string? Html { get; init; }
    [JsonPropertyName("sender_id")] public string? SenderId { get; init; }
    [JsonPropertyName("sender_display_name")] public string? SenderDisplayName { get; init; }
    [JsonPropertyName("timestamp_utc")] public required string TimestampUtc { get; init; }
    [JsonPropertyName("conversation_reference_id")] public string? ConversationReferenceId { get; init; }
    public List<Dictionary<string, object?>> Attachments { get; init; } = new();
    public List<Dictionary<string, object?>> Mentions { get; init; } = new();
    [JsonPropertyName("reply_to_message_id")] public string? ReplyToMessageId { get; init; }
    [JsonPropertyName("from_bot")] public bool FromBot { get; init; }
    public Dictionary<string, object?>? Raw { get; init; }

    /// <summary>
    /// "meeting_chat", "channel", "group_chat", or "personal".
    /// </summary>
    [JsonPropertyName("conversation_kind")] public string? ConversationKind { get; init; }

    /// <summary>Teams team (group) id when the activity is in a team channel.</summary>
    [JsonPropertyName("team_id")] public string? TeamId { get; init; }

    /// <summary>Teams channel id when the activity is in a team channel.</summary>
    [JsonPropertyName("channel_id")] public string? ChannelId { get; init; }
}
