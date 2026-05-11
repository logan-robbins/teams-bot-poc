using System.Text.Json.Serialization;

namespace TeamsMediaBot.Models;

/// <summary>
/// Payload of a <see cref="AlfredEventTypes.TranscriptOfficial"/>
/// envelope. Microsoft's own meeting transcript, fetched from Graph
/// after a meeting ends. Lines up speakers with the canonical text
/// the Teams in-product captions showed.
/// </summary>
public sealed record OfficialTranscriptPayload
{
    /// <summary>Graph onlineMeeting id this transcript belongs to.</summary>
    [JsonPropertyName("meeting_id")]
    public required string MeetingId { get; init; }

    /// <summary>Graph callTranscript id.</summary>
    [JsonPropertyName("transcript_id")]
    public required string TranscriptId { get; init; }

    /// <summary>AAD user id of the meeting organizer (scope for the fetch).</summary>
    [JsonPropertyName("organizer_oid")]
    public string? OrganizerOid { get; init; }

    /// <summary>ISO 8601 timestamp the transcript was produced.</summary>
    [JsonPropertyName("created_at_utc")]
    public string? CreatedAtUtc { get; init; }

    /// <summary>Total number of cues in the VTT.</summary>
    [JsonPropertyName("cue_count")]
    public int CueCount { get; init; }

    /// <summary>Parsed cues in source order.</summary>
    [JsonPropertyName("cues")]
    public required IReadOnlyList<OfficialTranscriptCue> Cues { get; init; }

    /// <summary>Original VTT body. Useful if a consumer wants to re-parse or archive verbatim.</summary>
    [JsonPropertyName("vtt_raw")]
    public string? VttRaw { get; init; }
}

/// <summary>One cue from a Teams meeting transcript VTT file.</summary>
public sealed record OfficialTranscriptCue
{
    /// <summary>Speaker name as Teams rendered it (e.g. "Logan Robbins").</summary>
    [JsonPropertyName("speaker")]
    public string? Speaker { get; init; }

    /// <summary>Plain-text cue body, <c>&lt;v Speaker&gt;…&lt;/v&gt;</c> markup stripped.</summary>
    [JsonPropertyName("text")]
    public required string Text { get; init; }

    /// <summary>Start time in milliseconds from start of the transcript.</summary>
    [JsonPropertyName("start_ms")]
    public long StartMs { get; init; }

    /// <summary>End time in milliseconds from start of the transcript.</summary>
    [JsonPropertyName("end_ms")]
    public long EndMs { get; init; }
}
