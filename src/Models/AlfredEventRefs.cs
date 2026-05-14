using System.Text.Json.Serialization;

namespace TeamsMediaBot.Models;

/// <summary>
/// Channel reference block on a <see cref="AlfredEventEnvelope"/>.
/// Mirrors <c>/teams/{team_id}/channels/{channel_id}/messages/{thread_id}/replies/{message_id}</c>.
///
/// <see cref="ThreadId"/> is the root message id of the channel thread
/// (Teams forces every channel post to be a thread root). For
/// <c>channel.message.*</c> events the <see cref="MessageId"/> is the
/// id of the specific reply (or equal to <see cref="ThreadId"/> when
/// the event is the root post). For <c>channel.attached</c> /
/// <c>channel.detached</c>, <see cref="ThreadId"/> and
/// <see cref="MessageId"/> are null.
/// </summary>
public sealed record ChannelRef
{
    [JsonPropertyName("team_id")]
    public required string TeamId { get; init; }

    /// <summary>Best-effort, populated when known. Resolved via <c>GET /teams/{id}</c> and cached.</summary>
    [JsonPropertyName("team_display_name")]
    public string? TeamDisplayName { get; init; }

    [JsonPropertyName("channel_id")]
    public required string ChannelId { get; init; }

    /// <summary>Best-effort, populated when known.</summary>
    [JsonPropertyName("channel_display_name")]
    public string? ChannelDisplayName { get; init; }

    /// <summary>Root message id of the thread. Required on chat events; null on channel-lifecycle events.</summary>
    [JsonPropertyName("thread_id")]
    public string? ThreadId { get; init; }

    /// <summary>Specific reply id. Equal to <see cref="ThreadId"/> when this event is the thread's root post.</summary>
    [JsonPropertyName("message_id")]
    public string? MessageId { get; init; }
}

/// <summary>
/// Meeting reference block on a <see cref="AlfredEventEnvelope"/>.
///
/// <see cref="MeetingId"/> is the Graph <c>onlineMeeting</c> id —
/// the canonical, stable key. The meeting chat thread id, the call
/// instance id, and the optional channel link are all sub-resources
/// of the meeting.
/// </summary>
public sealed record MeetingRef
{
    /// <summary>Graph onlineMeeting id (URL-safe base64). The canonical key.</summary>
    [JsonPropertyName("meeting_id")]
    public required string MeetingId { get; init; }

    /// <summary>Chat container id (<c>19:meeting_xxx@thread.v2</c>).</summary>
    [JsonPropertyName("meeting_chat_thread_id")]
    public string? MeetingChatThreadId { get; init; }

    /// <summary>Ephemeral call instance id; populated only while the bot is in-call.</summary>
    [JsonPropertyName("call_id")]
    public string? CallId { get; init; }

    /// <summary>Best-effort human-readable subject from <c>onlineMeeting.subject</c>.</summary>
    [JsonPropertyName("subject")]
    public string? Subject { get; init; }

    /// <summary>Meeting organizer.</summary>
    [JsonPropertyName("organizer")]
    public SenderRef? Organizer { get; init; }

    [JsonPropertyName("scheduled_start_utc")]
    public string? ScheduledStartUtc { get; init; }

    [JsonPropertyName("scheduled_end_utc")]
    public string? ScheduledEndUtc { get; init; }

    /// <summary>
    /// Populated after the meeting is linked to a channel (via
    /// <see cref="AlfredEventTypes.MeetingLinked"/>). Consumers should
    /// treat the linked ids as authoritative for rollup once present.
    /// </summary>
    [JsonPropertyName("channel_link")]
    public ChannelLink? ChannelLink { get; init; }
}

/// <summary>
/// Optional channel linkage stamped on every <see cref="MeetingRef"/>
/// after the bot learns which channel a meeting belongs to. Lets
/// consumers roll meetings up under their parent channel — and,
/// optionally, under a specific thread within that channel.
/// </summary>
public sealed record ChannelLink
{
    [JsonPropertyName("team_id")]
    public required string TeamId { get; init; }

    [JsonPropertyName("team_display_name")]
    public string? TeamDisplayName { get; init; }

    [JsonPropertyName("channel_id")]
    public required string ChannelId { get; init; }

    [JsonPropertyName("channel_display_name")]
    public string? ChannelDisplayName { get; init; }

    /// <summary>Optional thread granularity within the channel.</summary>
    [JsonPropertyName("thread_id")]
    public string? ThreadId { get; init; }

    [JsonPropertyName("linked_at_utc")]
    public required string LinkedAtUtc { get; init; }

    /// <summary><c>bot_framework_channeldata</c> | <c>manual_command</c> | <c>auto_detect</c>.</summary>
    [JsonPropertyName("linked_source")]
    public required string LinkedSource { get; init; }
}
