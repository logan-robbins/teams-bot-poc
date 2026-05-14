namespace TeamsMediaBot.Models;

/// <summary>
/// Provider-agnostic transcript event. Retained for backward-compat with
/// BlobEventArchive audit log reads; new code emits <see cref="MeetingTranscriptPayload"/>
/// envelopes directly.
/// </summary>
public record TranscriptEvent(
    /// <summary>"partial" | "final" | "session_started" | "session_stopped" | "error"</summary>
    string EventType,

    /// <summary>Transcribed text (null for non-text events)</summary>
    string? Text,

    /// <summary>ISO 8601 UTC timestamp</summary>
    string TimestampUtc,

    /// <summary>Normalized speaker ID: "speaker_0", "speaker_1", etc.</summary>
    string? SpeakerId = null,

    double? AudioStartMs = null,
    double? AudioEndMs = null,
    float? Confidence = null,
    List<WordDetail>? Words = null,
    EventMetadata? Metadata = null,
    EventError? Error = null,
    uint? DominantMediaSourceId = null,
    uint[]? ActiveMediaSourceIds = null
);

public record WordDetail(
    string Word,
    double StartMs,
    double EndMs,
    float? Confidence = null,
    string? SpeakerId = null
);

public record EventMetadata(
    string Provider,
    string? Model = null,
    string? SessionId = null
);

public record EventError(
    string Code,
    string Message
);
