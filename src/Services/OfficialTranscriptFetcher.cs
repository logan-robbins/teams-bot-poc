using System.Collections.Concurrent;
using System.Net;
using System.Text.Json;
using System.Text.RegularExpressions;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// After Alfred joins a channel meeting, registers a background poller
/// that watches Graph for the meeting's "Record and Transcribe"
/// transcript to land, then publishes it as a
/// <see cref="AlfredEventTypes.TranscriptOfficial"/> envelope through
/// the fan-out dispatcher.
///
/// <para>
/// We poll the <b>app-scoped</b> Graph resource
/// <c>appCatalogs/teamsApps/{teamsAppId}/installedToOnlineMeetings/getAllTranscripts</c>
/// every 60s starting 60s after the join, timing out at 30 min. This
/// resource is gated by the RSC <c>OnlineMeetingTranscript.Read.Chat</c>
/// declared in the manifest and consented at team-install time — so no
/// tenant-wide Entra application permission is required. The first
/// transcript whose <c>createdDateTime</c> is after the join time is
/// treated as the transcript for that call.
/// </para>
/// </summary>
public sealed partial class OfficialTranscriptFetcher : IAsyncDisposable
{
    private static readonly TimeSpan InitialDelay = TimeSpan.FromSeconds(60);
    private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(60);
    private static readonly TimeSpan PollDuration = TimeSpan.FromMinutes(30);

    private static readonly JsonSerializerOptions SerializerOptions = new(JsonSerializerDefaults.Web);

    private readonly EventFanoutDispatcher _dispatcher;
    private readonly GraphApiClient _graph;
    private readonly BotConfiguration _botConfig;
    private readonly BlobEventArchive? _blobArchive;
    private readonly ILogger<OfficialTranscriptFetcher> _logger;
    private readonly CancellationTokenSource _cts = new();
    private readonly ConcurrentDictionary<string, Task> _activeFetches = new(StringComparer.Ordinal);
    private bool _disposed;

    public OfficialTranscriptFetcher(
        EventFanoutDispatcher dispatcher,
        GraphApiClient graph,
        BotConfiguration botConfig,
        ILogger<OfficialTranscriptFetcher> logger,
        BlobEventArchive? blobArchive = null)
    {
        _dispatcher = dispatcher;
        _graph = graph;
        _botConfig = botConfig;
        _blobArchive = blobArchive;
        _logger = logger;
    }

    /// <summary>
    /// Schedule a one-shot post-meeting fetch. Idempotent on
    /// <paramref name="botCallId"/>; subsequent calls with the same id
    /// are no-ops.
    /// </summary>
    public void Register(
        string botCallId,
        string organizerOid,
        string teamId,
        string channelId,
        string channelThreadId,
        DateTimeOffset registeredAtUtc)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        if (string.IsNullOrWhiteSpace(botCallId) ||
            string.IsNullOrWhiteSpace(organizerOid))
        {
            _logger.LogDebug(
                "OfficialTranscriptFetcher.Register skipped: missing botCallId or organizerOid (botCallId={CallId}).",
                botCallId);
            return;
        }

        var pending = new PendingFetch(
            botCallId, organizerOid, teamId, channelId, channelThreadId, registeredAtUtc);

        _activeFetches.GetOrAdd(botCallId, _ => Task.Run(() => RunAsync(pending, _cts.Token)));
    }

    private async Task RunAsync(PendingFetch pending, CancellationToken cancellationToken)
    {
        try
        {
            await Task.Delay(InitialDelay, cancellationToken).ConfigureAwait(false);

            var deadline = DateTimeOffset.UtcNow + PollDuration;
            while (DateTimeOffset.UtcNow < deadline)
            {
                cancellationToken.ThrowIfCancellationRequested();

                var (meetingId, transcriptId, createdAt) =
                    await TryFindTranscriptAsync(pending, cancellationToken).ConfigureAwait(false);

                if (!string.IsNullOrEmpty(meetingId) && !string.IsNullOrEmpty(transcriptId))
                {
                    var vtt = await FetchVttAsync(meetingId!, transcriptId!, cancellationToken)
                        .ConfigureAwait(false);
                    if (!string.IsNullOrWhiteSpace(vtt))
                    {
                        await EmitAsync(pending, meetingId!, transcriptId!, createdAt, vtt, cancellationToken)
                            .ConfigureAwait(false);
                        return;
                    }
                }

                try
                {
                    await Task.Delay(PollInterval, cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    return;
                }
            }

            _logger.LogWarning(
                "Official transcript fetch timed out after {Mins} min for botCallId={CallId} team={TeamId} channel={ChannelId}.",
                (int)PollDuration.TotalMinutes, pending.BotCallId, pending.TeamId, pending.ChannelId);
        }
        catch (OperationCanceledException) { /* shutdown */ }
        catch (Exception ex)
        {
            _logger.LogError(ex,
                "Official transcript fetch crashed for botCallId={CallId} team={TeamId} channel={ChannelId}.",
                pending.BotCallId, pending.TeamId, pending.ChannelId);
        }
        finally
        {
            _activeFetches.TryRemove(pending.BotCallId, out _);
        }
    }

    private async Task<(string? MeetingId, string? TranscriptId, string? CreatedAt)>
        TryFindTranscriptAsync(PendingFetch pending, CancellationToken cancellationToken)
    {
        // ISO 8601 with millisecond precision, no offset (Graph requires UTC `Z`).
        var sinceIso = pending.RegisteredAtUtc.UtcDateTime.ToString("yyyy-MM-ddTHH:mm:ss.fffZ");
        var encodedSince = WebUtility.UrlEncode(sinceIso);
        var teamsAppId = _botConfig.AppId ?? string.Empty;
        // App-scoped endpoint — gated by the OnlineMeetingTranscript.Read.Chat
        // RSC consented at team install, NOT by any tenant-wide app permission.
        // appCatalogs/.../installedToOnlineMeetings/getAllTranscripts only
        // exists on the Graph BETA channel; v1.0 returns 400 "Resource not
        // found for the segment 'installedToOnlineMeetings'." so build an
        // absolute URL pinned to /beta/.
        var resource =
            $"https://graph.microsoft.com/beta/appCatalogs/teamsApps/{Uri.EscapeDataString(teamsAppId)}/installedToOnlineMeetings/getAllTranscripts" +
            $"?$filter=createdDateTime ge {encodedSince}&$orderby=createdDateTime asc&$top=5";

        try
        {
            using var doc = await _graph.GetResourceAsync(resource, cancellationToken).ConfigureAwait(false);
            if (!doc.RootElement.TryGetProperty("value", out var arr) || arr.ValueKind != JsonValueKind.Array)
            {
                return (null, null, null);
            }
            foreach (var item in arr.EnumerateArray())
            {
                var meetingId = item.TryGetProperty("meetingId", out var m) ? m.GetString() : null;
                var transcriptId = item.TryGetProperty("id", out var i) ? i.GetString() : null;
                var createdAt = item.TryGetProperty("createdDateTime", out var c) ? c.GetString() : null;
                if (!string.IsNullOrEmpty(meetingId) && !string.IsNullOrEmpty(transcriptId))
                {
                    return (meetingId, transcriptId, createdAt);
                }
            }
        }
        catch (GraphApiException ex) when (ex.StatusCode is HttpStatusCode.NotFound or HttpStatusCode.Forbidden)
        {
            _logger.LogDebug(
                "getAllTranscripts returned {Status} for app={AppId}; polling will retry.",
                ex.StatusCode, _botConfig.AppId);
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex,
                "getAllTranscripts probe failed for app={AppId}; polling will retry.",
                _botConfig.AppId);
        }

        return (null, null, null);
    }

    private async Task<string?> FetchVttAsync(
        string meetingId,
        string transcriptId,
        CancellationToken cancellationToken)
    {
        var teamsAppId = _botConfig.AppId ?? string.Empty;
        // App-scoped content fetch — same RSC as the list call, same
        // beta-only endpoint pinning.
        var resource =
            $"https://graph.microsoft.com/beta/appCatalogs/teamsApps/{Uri.EscapeDataString(teamsAppId)}/installedToOnlineMeetings/" +
            $"{Uri.EscapeDataString(meetingId)}/transcripts/{Uri.EscapeDataString(transcriptId)}/content" +
            "?$format=text/vtt";
        try
        {
            return await _graph.GetResourceTextAsync(resource, "text/vtt", cancellationToken)
                .ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex,
                "Failed to fetch transcript content meetingId={MeetingId} transcriptId={TranscriptId}",
                meetingId, transcriptId);
            return null;
        }
    }

    private async Task EmitAsync(
        PendingFetch pending,
        string meetingId,
        string transcriptId,
        string? createdAt,
        string vtt,
        CancellationToken cancellationToken)
    {
        var cues = ParseVtt(vtt);
        var payload = new OfficialTranscriptPayload
        {
            MeetingId = meetingId,
            TranscriptId = transcriptId,
            OrganizerOid = pending.OrganizerOid,
            CreatedAtUtc = createdAt,
            CueCount = cues.Count,
            Cues = cues,
            VttRaw = vtt,
        };

        var envelopeChatThreadId = string.IsNullOrWhiteSpace(pending.ChannelThreadId)
            ? meetingId
            : pending.ChannelThreadId;

        await _dispatcher.PublishAsync(new AlfredEventEnvelope
        {
            EventType = AlfredEventTypes.TranscriptOfficial,
            EventId = Guid.NewGuid().ToString("N"),
            Ts = createdAt ?? DateTimeOffset.UtcNow.ToString("O"),
            TeamId = string.IsNullOrWhiteSpace(pending.TeamId) ? null : pending.TeamId,
            ChannelId = string.IsNullOrWhiteSpace(pending.ChannelId) ? null : pending.ChannelId,
            ChatThreadId = envelopeChatThreadId,
            ChannelThreadId = string.IsNullOrWhiteSpace(pending.ChannelThreadId) ? null : pending.ChannelThreadId,
            Payload = payload,
        }, cancellationToken).ConfigureAwait(false);

        // Also drop the canonical Microsoft VTT transcript as a single
        // flat _official-transcript.txt in the meeting's blob folder so
        // an operator can grab the whole meeting in one download without
        // walking through per-event chunks. Fire-and-forget; archive
        // failures are swallowed inside ArchiveOfficialTranscriptAsync.
        if (_blobArchive is { IsEnabled: true })
        {
            _ = _blobArchive.ArchiveOfficialTranscriptAsync(envelopeChatThreadId, vtt, cancellationToken);
        }

        _logger.LogInformation(
            "Emitted transcript.official meetingId={MeetingId} transcriptId={TranscriptId} cues={CueCount} (botCallId={CallId})",
            meetingId, transcriptId, cues.Count, pending.BotCallId);
    }

    private static List<OfficialTranscriptCue> ParseVtt(string vtt)
    {
        var cues = new List<OfficialTranscriptCue>();
        var lines = vtt.Replace("\r\n", "\n").Split('\n');
        for (var i = 0; i < lines.Length; i++)
        {
            var line = lines[i];
            var timing = TimingRegex().Match(line);
            if (!timing.Success) continue;

            var startMs = ParseTimestampMs(timing.Groups["start"].Value);
            var endMs = ParseTimestampMs(timing.Groups["end"].Value);

            // Concatenate subsequent non-blank lines into the cue body
            // until a blank line or EOF.
            var body = new System.Text.StringBuilder();
            for (var j = i + 1; j < lines.Length; j++)
            {
                var bodyLine = lines[j];
                if (string.IsNullOrWhiteSpace(bodyLine))
                {
                    i = j;
                    break;
                }
                if (body.Length > 0) body.Append(' ');
                body.Append(bodyLine);
                i = j;
            }

            var raw = body.ToString();
            var voice = VoiceRegex().Match(raw);
            string? speaker = null;
            string text;
            if (voice.Success)
            {
                speaker = voice.Groups["speaker"].Value.Trim();
                text = voice.Groups["text"].Value.Trim();
            }
            else
            {
                text = raw.Trim();
            }

            if (text.Length == 0) continue;

            cues.Add(new OfficialTranscriptCue
            {
                Speaker = speaker,
                Text = text,
                StartMs = startMs,
                EndMs = endMs,
            });
        }
        return cues;
    }

    private static long ParseTimestampMs(string ts)
    {
        // Accepts "HH:MM:SS.fff" and "MM:SS.fff" forms; negative offsets allowed
        // (Graph emits negatives when transcription begins mid-call).
        var negative = ts.StartsWith('-');
        if (negative) ts = ts[1..];
        var parts = ts.Split(':');
        long h = 0, m = 0;
        string sec;
        if (parts.Length == 3) { h = long.Parse(parts[0]); m = long.Parse(parts[1]); sec = parts[2]; }
        else if (parts.Length == 2) { m = long.Parse(parts[0]); sec = parts[1]; }
        else return 0;
        var secParts = sec.Split('.');
        var s = long.Parse(secParts[0]);
        var ms = secParts.Length > 1 ? long.Parse(secParts[1].PadRight(3, '0')[..3]) : 0;
        var total = h * 3600_000 + m * 60_000 + s * 1000 + ms;
        return negative ? -total : total;
    }

    [GeneratedRegex(@"^(?<start>-?\d{1,2}:\d{2}:\d{2}\.\d{1,3}|-?\d{1,2}:\d{2}\.\d{1,3})\s*-->\s*(?<end>-?\d{1,2}:\d{2}:\d{2}\.\d{1,3}|-?\d{1,2}:\d{2}\.\d{1,3})",
        RegexOptions.Compiled)]
    private static partial Regex TimingRegex();

    [GeneratedRegex(@"<v\s+(?<speaker>[^>]+)>(?<text>.*?)</v>", RegexOptions.Compiled | RegexOptions.Singleline)]
    private static partial Regex VoiceRegex();

    public async ValueTask DisposeAsync()
    {
        if (_disposed) return;
        _disposed = true;
        _cts.Cancel();
        var tasks = _activeFetches.Values.ToArray();
        foreach (var t in tasks)
        {
            try { await t.ConfigureAwait(false); } catch { /* shutdown */ }
        }
        _cts.Dispose();
    }

    private readonly record struct PendingFetch(
        string BotCallId,
        string OrganizerOid,
        string TeamId,
        string ChannelId,
        string ChannelThreadId,
        DateTimeOffset RegisteredAtUtc);
}
