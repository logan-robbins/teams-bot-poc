using System.Text.Json.Serialization;

namespace TeamsMediaBot.Models;

/// <summary>
/// Payload carried inside an <see cref="AlfredEventEnvelope"/> with
/// <c>event_type = "system.session_linked"</c>. Notifies consumers
/// that a meeting <c>chat_thread_id</c> has been bound to a parent
/// <c>(team_id, channel_id)</c> so prior events for that thread can
/// be rolled up under the channel.
/// </summary>
public sealed record SessionLinkedPayload
{
    [JsonPropertyName("chat_thread_id")] public required string ChatThreadId { get; init; }
    [JsonPropertyName("team_id")] public required string TeamId { get; init; }
    [JsonPropertyName("channel_id")] public required string ChannelId { get; init; }
    [JsonPropertyName("channel_thread_id")] public string? ChannelThreadId { get; init; }
    [JsonPropertyName("source")] public string? Source { get; init; }
}
