using System.Text.Json.Serialization;

namespace TeamsMediaBot.Models;

/// <summary>
/// Payload for <see cref="AlfredEventTypes.MeetingCreated"/> and
/// <see cref="AlfredEventTypes.MeetingEnded"/>. The envelope's
/// <see cref="AlfredEventEnvelope.MeetingRef"/> carries the
/// <c>meeting_id</c> and <c>meeting_chat_thread_id</c>.
/// </summary>
public sealed record MeetingLifecyclePayload
{
    [JsonPropertyName("subject")]
    public string? Subject { get; init; }

    [JsonPropertyName("organizer")]
    public SenderRef? Organizer { get; init; }

    [JsonPropertyName("scheduled_start_utc")]
    public string? ScheduledStartUtc { get; init; }

    [JsonPropertyName("scheduled_end_utc")]
    public string? ScheduledEndUtc { get; init; }

    /// <summary>Populated on <c>meeting.ended</c> when the bot was in-call.</summary>
    [JsonPropertyName("actual_start_utc")]
    public string? ActualStartUtc { get; init; }

    [JsonPropertyName("actual_end_utc")]
    public string? ActualEndUtc { get; init; }
}

/// <summary>
/// Payload for <see cref="AlfredEventTypes.MeetingCallJoined"/> and
/// <see cref="AlfredEventTypes.MeetingCallLeft"/>. The envelope's
/// <see cref="AlfredEventEnvelope.MeetingRef"/> carries the
/// <c>meeting_id</c> and <c>call_id</c>.
/// </summary>
public sealed record MeetingCallPayload
{
    [JsonPropertyName("join_url")]
    public string? JoinUrl { get; init; }

    /// <summary>
    /// <c>graph_join</c> | <c>policy_auto_invite</c> | <c>invite_and_graph_join</c>.
    /// </summary>
    [JsonPropertyName("join_mode")]
    public string? JoinMode { get; init; }
}
