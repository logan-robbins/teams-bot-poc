using System.Text.Json.Serialization;

namespace TeamsMediaBot.Models;

/// <summary>
/// alfred-v2 event envelope. POSTed to every registered consumer URL
/// and mirrored to the blob archive. Stable contract: additive-only
/// within v2; breaking changes ship as v3.
///
/// Every envelope is unambiguously either a channel event or a
/// meeting event. Exactly one of <see cref="ChannelRef"/> and
/// <see cref="MeetingRef"/> is populated, decided by the event type:
/// <c>channel.*</c> events populate <see cref="ChannelRef"/>;
/// <c>meeting.*</c> events populate <see cref="MeetingRef"/>.
///
/// Full schema in <c>docs/event-contract.md</c>.
/// </summary>
public sealed record AlfredEventEnvelope
{
    [JsonPropertyName("schema_version")]
    public string SchemaVersion { get; init; } = "alfred-v2";

    /// <summary>One of the <see cref="AlfredEventTypes"/> constants.</summary>
    [JsonPropertyName("event_type")]
    public required string EventType { get; init; }

    /// <summary>Stable per-event id. Consumers dedupe on retry by this.</summary>
    [JsonPropertyName("event_id")]
    public required string EventId { get; init; }

    /// <summary>ISO 8601 UTC.</summary>
    [JsonPropertyName("ts")]
    public required string Ts { get; init; }

    /// <summary>Populated iff <see cref="EventType"/> starts with <c>channel.</c>.</summary>
    [JsonPropertyName("channel_ref")]
    public ChannelRef? ChannelRef { get; init; }

    /// <summary>Populated iff <see cref="EventType"/> starts with <c>meeting.</c>.</summary>
    [JsonPropertyName("meeting_ref")]
    public MeetingRef? MeetingRef { get; init; }

    /// <summary>
    /// Bot Framework conversation reference id. Echo back to
    /// <c>POST $BOT/api/send-chat</c> to post into the same chat.
    /// Null on system events that are not chat-bound.
    /// </summary>
    [JsonPropertyName("conversation_reference_id")]
    public string? ConversationReferenceId { get; init; }

    /// <summary>Event-type-specific payload. See docs/event-contract.md §3.</summary>
    [JsonPropertyName("payload")]
    public required object Payload { get; init; }
}

/// <summary>
/// All <see cref="AlfredEventEnvelope.EventType"/> string constants.
/// Names mirror the Graph URL hierarchy.
/// </summary>
public static class AlfredEventTypes
{
    // Channel lifecycle
    public const string ChannelAttached = "channel.attached";
    public const string ChannelDetached = "channel.detached";

    // Channel chat
    public const string ChannelMessageCreated = "channel.message.created";
    public const string ChannelMessageUpdated = "channel.message.updated";
    public const string ChannelMessageDeleted = "channel.message.deleted";

    // Meeting lifecycle
    public const string MeetingCreated   = "meeting.created";
    public const string MeetingEnded     = "meeting.ended";
    public const string MeetingLinked    = "meeting.linked";
    public const string MeetingCallJoined = "meeting.call.joined";
    public const string MeetingCallLeft   = "meeting.call.left";

    // Meeting chat
    public const string MeetingChatCreated = "meeting.chat.created";
    public const string MeetingChatUpdated = "meeting.chat.updated";
    public const string MeetingChatDeleted = "meeting.chat.deleted";

    // Meeting transcripts
    public const string MeetingTranscriptPartial  = "meeting.transcript.partial";
    public const string MeetingTranscriptFinal    = "meeting.transcript.final";
    public const string MeetingTranscriptOfficial = "meeting.transcript.official";
}
