using System.Text.Json.Serialization;

namespace TeamsMediaBot.Models;

/// <summary>
/// Payload for <see cref="AlfredEventTypes.ChannelAttached"/> and
/// <see cref="AlfredEventTypes.ChannelDetached"/>. The envelope's
/// <see cref="AlfredEventEnvelope.ChannelRef"/> carries the
/// <c>(team_id, channel_id)</c> tuple.
/// </summary>
public sealed record ChannelLifecyclePayload
{
    /// <summary>AAD object id of the user who installed/uninstalled.</summary>
    [JsonPropertyName("installed_by")]
    public string? InstalledBy { get; init; }

    /// <summary>Teams app installation id, when known.</summary>
    [JsonPropertyName("installation_id")]
    public string? InstallationId { get; init; }

    /// <summary><c>standard</c> | <c>private</c> | <c>shared</c>.</summary>
    [JsonPropertyName("membership_type")]
    public string? MembershipType { get; init; }
}
