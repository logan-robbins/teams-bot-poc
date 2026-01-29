namespace TeamsMediaBot.Models;

/// <summary>
/// Transcript event sent to Python agent
/// Based on Part I of the validated guide
/// </summary>
public record TranscriptEvent(
    string Kind,      // "recognizing" | "recognized" | "session_started" | "session_stopped" | "canceled"
    string? Text,     // Transcript text (null for status events)
    string TsUtc,     // ISO 8601 UTC timestamp
    string? Details = null  // Additional info (e.g., error details for canceled events)
);
