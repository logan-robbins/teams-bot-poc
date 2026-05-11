using System.Net;

namespace TeamsMediaBot.Services;

/// <summary>
/// Synthesizes the canonical channel-meeting join URL. Channel meetings
/// live in the channel's persistent meeting room (one thread, many
/// sequential meeting instances), so this URL resolves to the active
/// call regardless of which specific meeting started it.
/// </summary>
internal static class ChannelMeetingJoinUrls
{
    public static string Build(string channelThreadId, string tenantId, string organizerOid)
    {
        var encodedThread = WebUtility.UrlEncode(channelThreadId);
        var oid = string.IsNullOrWhiteSpace(organizerOid) ? string.Empty : organizerOid;
        var contextJson = $"{{\"Tid\":\"{tenantId}\",\"Oid\":\"{oid}\",\"MessageId\":\"0\"}}";
        var encodedContext = WebUtility.UrlEncode(contextJson);
        return $"https://teams.microsoft.com/l/meetup-join/{encodedThread}/0?context={encodedContext}";
    }
}
