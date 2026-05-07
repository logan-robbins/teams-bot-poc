namespace TeamsMediaBot.Models;

/// <summary>
/// Provider-agnostic transcript event with diarization support.
/// Normalized from Azure ConversationTranscriber to this common format.
/// </summary>
public record TranscriptEvent(
    /// <summary>"partial" | "final" | "session_started" | "session_stopped" | "error"</summary>
    string EventType,

    /// <summary>Transcribed text (null for non-text events)</summary>
    string? Text,

    /// <summary>ISO 8601 UTC timestamp</summary>
    string TimestampUtc,

    /// <summary>Teams chat thread id (meeting id). Required so the sink can route per-meeting.</summary>
    string? ChatThreadId = null,

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
    EventError? Error = null,

    /// <summary>
    /// E3: Teams MediaSourceId most recently flagged dominant by the
    /// Graph Communications Media SDK at publish time. Null when no
    /// dominant-speaker hint was available for this buffer.
    /// </summary>
    uint? DominantMediaSourceId = null,

    /// <summary>
    /// E3: Snapshot of MediaSourceIds active in the buffer at publish
    /// time, sourced from AudioMediaBuffer.ActiveSpeakers.
    /// </summary>
    uint[]? ActiveMediaSourceIds = null,

    /// <summary>
    /// Teams team (group) id, when the bot has learned that this
    /// meeting was spawned from a channel. Stamped on every transcript
    /// emitted for the call so the Python sink can group by channel
    /// without an extra join.
    /// </summary>
    string? TeamId = null,

    /// <summary>
    /// Teams channel id, paired with <see cref="TeamId"/>.
    /// </summary>
    string? ChannelId = null,

    /// <summary>
    /// Parent channel's conversation id (<c>19:{channelId}@thread.tacv2</c>).
    /// Lets meetings spawned from a channel roll up under it for
    /// later channel-wide analytics.
    /// </summary>
    string? ChannelThreadId = null
);

public record WordDetail(
    string Word,
    double StartMs,
    double EndMs,
    float? Confidence = null,
    string? SpeakerId = null
);

public record EventMetadata(
    /// <summary>"azure_speech" — the only sanctioned provider on Disney</summary>
    string Provider,
    string? Model = null,
    string? SessionId = null
);

public record EventError(
    string Code,
    string Message
);
