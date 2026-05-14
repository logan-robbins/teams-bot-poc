using System.Text.Json.Serialization;

namespace TeamsMediaBot.Models;

/// <summary>Identifies a chat sender or meeting organizer.</summary>
public sealed record SenderRef
{
    [JsonPropertyName("aad_id")]
    public string? AadId { get; init; }

    [JsonPropertyName("display_name")]
    public string? DisplayName { get; init; }

    /// <summary><c>user</c> | <c>bot</c> | <c>application</c>.</summary>
    [JsonPropertyName("kind")]
    public string? Kind { get; init; }
}

/// <summary>
/// Identifies a transcript speaker. <see cref="Id"/> is the
/// STT-provider speaker label (e.g. <c>speaker_0</c>); the AAD id and
/// display name are resolved when MSI ↔ AAD lookup succeeds via the
/// Graph Communications Media SDK.
/// </summary>
public sealed record SpeakerRef
{
    [JsonPropertyName("id")]
    public string? Id { get; init; }

    [JsonPropertyName("aad_id")]
    public string? AadId { get; init; }

    [JsonPropertyName("display_name")]
    public string? DisplayName { get; init; }
}

/// <summary>
/// Attachment metadata for chat messages (channel and meeting).
/// <see cref="BlobArchivePath"/> is populated after the bot mirrors
/// the file to the blob archive.
/// </summary>
public sealed record AttachmentRef
{
    [JsonPropertyName("attachment_id")]
    public required string AttachmentId { get; init; }

    [JsonPropertyName("name")]
    public string? Name { get; init; }

    [JsonPropertyName("content_type")]
    public string? ContentType { get; init; }

    [JsonPropertyName("size_bytes")]
    public long? SizeBytes { get; init; }

    [JsonPropertyName("graph_drive_item_id")]
    public string? GraphDriveItemId { get; init; }

    [JsonPropertyName("download_url")]
    public string? DownloadUrl { get; init; }

    [JsonPropertyName("blob_archive_path")]
    public string? BlobArchivePath { get; init; }
}

/// <summary>
/// Word-level transcript detail. Populated when the STT provider
/// exposes word timings + speaker attribution.
/// </summary>
public sealed record TranscriptWord
{
    [JsonPropertyName("word")]
    public required string Word { get; init; }

    [JsonPropertyName("start_ms")]
    public required double StartMs { get; init; }

    [JsonPropertyName("end_ms")]
    public required double EndMs { get; init; }

    [JsonPropertyName("confidence")]
    public float? Confidence { get; init; }

    [JsonPropertyName("speaker_id")]
    public string? SpeakerId { get; init; }
}

/// <summary>STT provider metadata stamped on every transcript chunk.</summary>
public sealed record TranscriptProvider
{
    [JsonPropertyName("name")]
    public required string Name { get; init; }

    [JsonPropertyName("model")]
    public string? Model { get; init; }

    [JsonPropertyName("session_id")]
    public string? SessionId { get; init; }
}

/// <summary>
/// Teams MediaSourceId snapshot from the Graph Communications Media
/// SDK at the moment a transcript buffer was published. Used to
/// reconcile speaker labels with AAD identities.
/// </summary>
public sealed record MediaSourceSnapshot
{
    [JsonPropertyName("dominant_id")]
    public uint? DominantId { get; init; }

    [JsonPropertyName("active_ids")]
    public uint[]? ActiveIds { get; init; }
}
