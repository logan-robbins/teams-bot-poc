using System.Text.Json.Serialization;

namespace TeamsMediaBot.Models;

/// <summary>
/// Wire shape carried inside an <see cref="AlfredEventEnvelope"/> with
/// <c>event_type = "chat.message"</c>. Mirrors the historical
/// <c>ChatMessageRequest</c> wire shape on the Python sink, retained
/// verbatim so reference consumers don't change.
///
/// <para>
/// <c>chat_thread_id</c> is the canonical session key. For meeting
/// chats it is <c>19:meeting_xxx@thread.v2</c>; for Teams channels it
/// is <c>19:{channel-id}@thread.tacv2</c> (or <c>@thread.skype</c> for
/// legacy channels). <c>conversation_kind</c> distinguishes them, and
/// <c>team_id</c> / <c>channel_id</c> are populated for channel
/// activities.
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

    /// <summary>"meeting_chat", "channel", "group_chat", or "personal".</summary>
    [JsonPropertyName("conversation_kind")] public string? ConversationKind { get; init; }

    /// <summary>Teams team (group) id when the activity is in a team channel.</summary>
    [JsonPropertyName("team_id")] public string? TeamId { get; init; }

    /// <summary>Teams channel id when the activity is in a team channel.</summary>
    [JsonPropertyName("channel_id")] public string? ChannelId { get; init; }

    /// <summary>
    /// Parent channel's conversation id (<c>19:{channelId}@thread.tacv2</c>).
    /// For channel posts equals <see cref="ChatThreadId"/>; for meeting
    /// chats spawned from a channel, points at the parent channel's
    /// thread so analytics can roll meetings under their channel.
    /// </summary>
    [JsonPropertyName("channel_thread_id")] public string? ChannelThreadId { get; init; }
}
