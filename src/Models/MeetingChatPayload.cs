using System.Text.Json.Serialization;

namespace TeamsMediaBot.Models;

/// <summary>
/// Payload for <see cref="AlfredEventTypes.MeetingChatCreated"/>,
/// <see cref="AlfredEventTypes.MeetingChatUpdated"/>, and
/// <see cref="AlfredEventTypes.MeetingChatDeleted"/>. The envelope's
/// <see cref="AlfredEventEnvelope.MeetingRef"/> carries the
/// <c>meeting_id</c> and <c>meeting_chat_thread_id</c>.
///
/// Identical shape to <see cref="ChannelMessagePayload"/> minus the
/// thread-root concept — meeting chat is a flat conversation.
/// </summary>
public sealed record MeetingChatPayload
{
    [JsonPropertyName("message_id")]
    public required string MessageId { get; init; }

    [JsonPropertyName("sender")]
    public required SenderRef Sender { get; init; }

    [JsonPropertyName("text")]
    public string? Text { get; init; }

    [JsonPropertyName("html")]
    public string? Html { get; init; }

    [JsonPropertyName("timestamp_utc")]
    public required string TimestampUtc { get; init; }

    [JsonPropertyName("reply_to_message_id")]
    public string? ReplyToMessageId { get; init; }

    [JsonPropertyName("from_bot")]
    public required bool FromBot { get; init; }

    [JsonPropertyName("attachments")]
    public IReadOnlyList<AttachmentRef> Attachments { get; init; } = Array.Empty<AttachmentRef>();

    [JsonPropertyName("mentions")]
    public IReadOnlyList<Dictionary<string, object?>> Mentions { get; init; } = Array.Empty<Dictionary<string, object?>>();

    [JsonPropertyName("raw")]
    public Dictionary<string, object?>? Raw { get; init; }
}
