namespace TeamsMediaBot.Models;

/// <summary>
/// Provider-agnostic transcript event with diarization support.
/// Normalized from Deepgram/Azure/etc to this common format.
/// 
/// Last Grunted: 01/31/2026 12:00:00 PM PST
/// </summary>
public record TranscriptEvent(
    /// <summary>"partial" | "final" | "session_started" | "session_stopped" | "error"</summary>
    string EventType,
    
    /// <summary>Transcribed text (null for non-text events)</summary>
    string? Text,
    
    /// <summary>ISO 8601 UTC timestamp</summary>
    string TimestampUtc,
    
    /// <summary>Normalized speaker ID: "speaker_0", "speaker_1", etc. Null if diarization disabled.</summary>
    string? SpeakerId = null,
    
    /// <summary>Segment start time in milliseconds from audio stream start</summary>
    double? AudioStartMs = null,
    
    /// <summary>Segment end time in milliseconds from audio stream start</summary>
    double? AudioEndMs = null,
    
    /// <summary>Confidence score 0.0-1.0</summary>
    float? Confidence = null,
    
    /// <summary>Word-level details with timestamps and speaker IDs</summary>
    List<WordDetail>? Words = null,
    
    /// <summary>Provider metadata</summary>
    EventMetadata? Metadata = null,
    
    /// <summary>Error details (only for error events)</summary>
    EventError? Error = null
);

public record WordDetail(
    string Word,
    double StartMs,
    double EndMs,
    float? Confidence = null,
    string? SpeakerId = null
);

public record EventMetadata(
    /// <summary>"deepgram" | "azure_speech"</summary>
    string Provider,
    string? Model = null,
    string? SessionId = null
);

public record EventError(
    string Code,
    string Message
);
