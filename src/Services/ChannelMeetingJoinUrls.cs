using System.Net;

namespace TeamsMediaBot.Services;

/// <summary>
/// Synthesizes the canonical Teams meetup-join URL from a Teams chat or
/// channel thread id. The context <c>Oid</c> must be a real tenant user
/// id, normally the organizer or call initiator; using the bot AppId
/// yields Graph 7504/7505 authorization failures even when the chat has
/// the right RSC grants.
/// </summary>
internal static class ChannelMeetingJoinUrls
{
    public static string Build(string channelThreadId, string tenantId, string organizerOid)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(channelThreadId);
        ArgumentException.ThrowIfNullOrWhiteSpace(tenantId);
        ArgumentException.ThrowIfNullOrWhiteSpace(organizerOid);

        var encodedThread = WebUtility.UrlEncode(channelThreadId);
        var contextJson = $"{{\"Tid\":\"{tenantId}\",\"Oid\":\"{organizerOid}\",\"MessageId\":\"0\"}}";
        var encodedContext = WebUtility.UrlEncode(contextJson);
        return $"https://teams.microsoft.com/l/meetup-join/{encodedThread}/0?context={encodedContext}";
    }
}
