using System.Text.Json.Serialization;

namespace TeamsMediaBot.Models;

/// <summary>
/// Payload for <see cref="AlfredEventTypes.MeetingTranscriptPartial"/>
/// and <see cref="AlfredEventTypes.MeetingTranscriptFinal"/>.
/// Real-time STT output from the bot's media stream. The envelope's
/// <see cref="AlfredEventEnvelope.MeetingRef"/> carries the
/// <c>meeting_id</c>.
/// </summary>
public sealed record MeetingTranscriptPayload
{
    [JsonPropertyName("text")]
    public required string Text { get; init; }

    [JsonPropertyName("timestamp_utc")]
    public required string TimestampUtc { get; init; }

    [JsonPropertyName("speaker")]
    public SpeakerRef? Speaker { get; init; }

    [JsonPropertyName("audio_start_ms")]
    public double? AudioStartMs { get; init; }

    [JsonPropertyName("audio_end_ms")]
    public double? AudioEndMs { get; init; }

    [JsonPropertyName("confidence")]
    public float? Confidence { get; init; }

    [JsonPropertyName("words")]
    public IReadOnlyList<TranscriptWord>? Words { get; init; }

    [JsonPropertyName("media_source")]
    public MediaSourceSnapshot? MediaSource { get; init; }

    [JsonPropertyName("provider")]
    public required TranscriptProvider Provider { get; init; }
}
