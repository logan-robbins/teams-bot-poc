using System.Text.Json.Serialization;

namespace TeamsMediaBot.Models;

/// <summary>
/// Payload for <see cref="AlfredEventTypes.MeetingTranscriptOfficial"/>.
/// Microsoft's post-meeting transcript fetched via Graph after a
/// private chat meeting ends with Record-and-Transcribe enabled.
/// Channel meetings do not produce this event.
/// </summary>
public sealed record MeetingOfficialTranscriptPayload
{
    /// <summary>Graph <c>callTranscript</c> id.</summary>
    [JsonPropertyName("transcript_id")]
    public required string TranscriptId { get; init; }

    /// <summary>AAD user id of the meeting organizer (scope for the fetch).</summary>
    [JsonPropertyName("organizer_oid")]
    public string? OrganizerOid { get; init; }

    [JsonPropertyName("fetched_at_utc")]
    public required string FetchedAtUtc { get; init; }

    [JsonPropertyName("created_at_utc")]
    public string? CreatedAtUtc { get; init; }

    /// <summary>Blob archive path to the raw WebVTT body.</summary>
    [JsonPropertyName("vtt_url")]
    public required string VttUrl { get; init; }

    /// <summary>Number of cues in <see cref="Cues"/>.</summary>
    [JsonPropertyName("cue_count")]
    public required int CueCount { get; init; }

    /// <summary>Parsed cues in source order.</summary>
    [JsonPropertyName("cues")]
    public required IReadOnlyList<OfficialTranscriptCue> Cues { get; init; }
}

/// <summary>One cue from a Teams meeting transcript VTT.</summary>
public sealed record OfficialTranscriptCue
{
    [JsonPropertyName("speaker")]
    public SpeakerRef? Speaker { get; init; }

    /// <summary>Plain-text cue body with <c>&lt;v Speaker&gt;…&lt;/v&gt;</c> markup stripped.</summary>
    [JsonPropertyName("text")]
    public required string Text { get; init; }

    [JsonPropertyName("start_ms")]
    public required long StartMs { get; init; }

    [JsonPropertyName("end_ms")]
    public required long EndMs { get; init; }
}
