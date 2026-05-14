using System.Text.Json.Serialization;

namespace TeamsMediaBot.Models;

/// <summary>
/// Payload for <see cref="AlfredEventTypes.MeetingLinked"/>. The bot
/// learned this meeting belongs to a channel. The
/// <see cref="AlfredEventEnvelope.MeetingRef"/> on this envelope and
/// every subsequent meeting envelope will carry the populated
/// <see cref="ChannelLink"/>.
///
/// Consumers should treat the link as authoritative and backfill any
/// prior events for this <c>meeting_id</c> under the linked channel.
/// </summary>
public sealed record MeetingLinkedPayload
{
    /// <summary><c>bot_framework_channeldata</c> | <c>manual_command</c> | <c>auto_detect</c>.</summary>
    [JsonPropertyName("linked_source")]
    public required string LinkedSource { get; init; }
}
