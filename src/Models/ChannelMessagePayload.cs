using System.Text.Json.Serialization;

namespace TeamsMediaBot.Models;

/// <summary>
/// Payload for <see cref="AlfredEventTypes.ChannelMessageCreated"/>,
/// <see cref="AlfredEventTypes.ChannelMessageUpdated"/>, and
/// <see cref="AlfredEventTypes.ChannelMessageDeleted"/>. The envelope's
/// <see cref="AlfredEventEnvelope.ChannelRef"/> carries the
/// <c>(team_id, channel_id, thread_id, message_id)</c> tuple.
/// </summary>
public sealed record ChannelMessagePayload
{
    [JsonPropertyName("sender")]
    public required SenderRef Sender { get; init; }

    [JsonPropertyName("text")]
    public string? Text { get; init; }

    [JsonPropertyName("html")]
    public string? Html { get; init; }

    [JsonPropertyName("timestamp_utc")]
    public required string TimestampUtc { get; init; }

    /// <summary>Id of the message this is a reply to (within the thread). Null on root posts.</summary>
    [JsonPropertyName("reply_to_message_id")]
    public string? ReplyToMessageId { get; init; }

    /// <summary>True iff this event is the thread's root post (message_id == thread_id).</summary>
    [JsonPropertyName("is_root")]
    public required bool IsRoot { get; init; }

    [JsonPropertyName("from_bot")]
    public required bool FromBot { get; init; }

    [JsonPropertyName("attachments")]
    public IReadOnlyList<AttachmentRef> Attachments { get; init; } = Array.Empty<AttachmentRef>();

    [JsonPropertyName("mentions")]
    public IReadOnlyList<Dictionary<string, object?>> Mentions { get; init; } = Array.Empty<Dictionary<string, object?>>();

    /// <summary>Full source payload. Best-effort.</summary>
    [JsonPropertyName("raw")]
    public Dictionary<string, object?>? Raw { get; init; }
}
