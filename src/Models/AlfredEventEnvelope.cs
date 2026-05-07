using System.Text.Json.Serialization;

namespace TeamsMediaBot.Models;

/// <summary>
/// Versioned envelope POSTed to every registered consumer for a
/// <c>(team_id, channel_id)</c>. Stable contract: additive-only within
/// <c>alfred-events-v1</c>; breaking changes ship as <c>v2</c>.
///
/// <para>
/// Routing keys live at the top level so a consumer can route on
/// <c>(team_id, channel_id)</c> or <c>chat_thread_id</c> without parsing
/// <see cref="Payload"/>. Payload shape varies by <see cref="EventType"/>
/// and is documented in <c>docs/event-contract.md</c>.
/// </para>
/// </summary>
public sealed record AlfredEventEnvelope
{
    [JsonPropertyName("schema_version")]
    public string SchemaVersion { get; init; } = "alfred-events-v1";

    /// <summary>One of the <see cref="AlfredEventTypes"/> string constants.</summary>
    [JsonPropertyName("event_type")]
    public required string EventType { get; init; }

    /// <summary>Stable per-event id. Consumers may use it to dedupe on retry.</summary>
    [JsonPropertyName("event_id")]
    public required string EventId { get; init; }

    /// <summary>ISO 8601 UTC.</summary>
    [JsonPropertyName("ts")]
    public required string Ts { get; init; }

    [JsonPropertyName("team_id")]
    public string? TeamId { get; init; }

    [JsonPropertyName("channel_id")]
    public string? ChannelId { get; init; }

    /// <summary>
    /// Canonical session key. <c>19:meeting_xxx@thread.v2</c> for a
    /// meeting; <c>19:{channelId}@thread.tacv2</c> for a channel post.
    /// Always present.
    /// </summary>
    [JsonPropertyName("chat_thread_id")]
    public required string ChatThreadId { get; init; }

    /// <summary>Parent channel's conversation id when known.</summary>
    [JsonPropertyName("channel_thread_id")]
    public string? ChannelThreadId { get; init; }

    /// <summary>
    /// Bot Framework conversation reference id. Echo this back to
    /// <c>POST $BOT/api/send-chat</c> to post into the same chat.
    /// </summary>
    [JsonPropertyName("conversation_reference_id")]
    public string? ConversationReferenceId { get; init; }

    /// <summary>Event-type-specific payload. See docs/event-contract.md.</summary>
    [JsonPropertyName("payload")]
    public required object Payload { get; init; }
}

/// <summary>
/// String constants for <see cref="AlfredEventEnvelope.EventType"/>.
/// </summary>
public static class AlfredEventTypes
{
    /// <summary>STT interim hypothesis. Payload: <see cref="TranscriptEvent"/>.</summary>
    public const string TranscriptPartial = "transcript.partial";

    /// <summary>STT finalized utterance. Payload: <see cref="TranscriptEvent"/>.</summary>
    public const string TranscriptFinal = "transcript.final";

    /// <summary>
    /// User chat message in a meeting chat or attached channel.
    /// Payload mirrors today's <c>ChatMessageRequest</c> wire shape.
    /// </summary>
    public const string ChatMessage = "chat.message";

    /// <summary>
    /// Bot just learned this <c>chat_thread_id</c> belongs to
    /// <c>(team_id, channel_id)</c>. Payload includes <c>source</c>.
    /// </summary>
    public const string SessionLinked = "system.session_linked";

    /// <summary>Channel attachment created. Payload: attachment record.</summary>
    public const string ChannelAttached = "system.channel_attached";

    /// <summary>Channel attachment removed. Payload: attachment record.</summary>
    public const string ChannelDetached = "system.channel_detached";
}
