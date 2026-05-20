using System.Collections.Concurrent;
using System.Globalization;
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
    // meetingId → organizerOid, so FetchVttAsync can reconstruct the
    // user-scoped URL after TryFindTranscriptAsync hands off just the
    // (meetingId, transcriptId) pair.
    private readonly ConcurrentDictionary<string, string> _activeFetchOrganizers = new(StringComparer.Ordinal);
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
        string meetingChatThreadId,
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

        var pending = new PendingFetch(botCallId, organizerOid, meetingChatThreadId, registeredAtUtc);
        _activeFetchOrganizers[botCallId] = organizerOid;

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
                "Official transcript fetch timed out after {Mins} min for botCallId={CallId} meetingChatThreadId={MeetingChatThreadId}.",
                (int)PollDuration.TotalMinutes, pending.BotCallId, pending.MeetingChatThreadId);
        }
        catch (OperationCanceledException) { /* shutdown */ }
        catch (Exception ex)
        {
            _logger.LogError(ex,
                "Official transcript fetch crashed for botCallId={CallId} meetingChatThreadId={MeetingChatThreadId}.",
                pending.BotCallId, pending.MeetingChatThreadId);
        }
        finally
        {
            _activeFetches.TryRemove(pending.BotCallId, out _);
            _activeFetchOrganizers.TryRemove(pending.BotCallId, out _);
        }
    }

    private async Task<(string? MeetingId, string? TranscriptId, string? CreatedAt)>
        TryFindTranscriptAsync(PendingFetch pending, CancellationToken cancellationToken)
    {
        // List transcripts for the (organizer, onlineMeeting) pair.
        // Matches the Microsoft sample at OfficeDev/Microsoft-Teams-Samples
        // (`samples/meetings-transcription/csharp`).
        //
        // Two non-obvious twists vs. the sample:
        // 1. The Graph URL needs the CANONICAL onlineMeeting.id (URL-safe
        //    base64 of `1*{tenantId}*0**{chatThreadId}`), not the
        //    `19:meeting_…@thread.v2` chat thread id. Derive deterministically
        //    from pending.BotCallId when it's still in chat-thread form.
        // 2. Append `?useResourceSpecificConsentBasedAuthorization=true`
        //    so Graph evaluates our chat-scoped RSC
        //    OnlineMeetingTranscript.Read.Chat (consented at "+Apps"
        //    install) instead of demanding the tenant-wide
        //    OnlineMeetingTranscript.Read.All. Without this flag the
        //    user-scoped URL returns 403 with empty RSC grants.
        if (string.IsNullOrWhiteSpace(pending.OrganizerOid)
            || string.IsNullOrWhiteSpace(pending.BotCallId))
        {
            return (null, null, null);
        }

        var canonicalMeetingId = ToCanonicalMeetingId(pending.BotCallId);
        // NOTE: Graph's per-meeting transcripts endpoint REJECTS $orderby
        // and $top with `400 Query option 'OrderBy' is not allowed`.
        // List everything; pick newest in-process below.
        var resource =
            $"https://graph.microsoft.com/v1.0/users/{Uri.EscapeDataString(pending.OrganizerOid)}" +
            $"/onlineMeetings/{Uri.EscapeDataString(canonicalMeetingId)}/transcripts" +
            "?useResourceSpecificConsentBasedAuthorization=true";

        try
        {
            using var doc = await _graph.GetResourceAsync(resource, cancellationToken).ConfigureAwait(false);
            if (!doc.RootElement.TryGetProperty("value", out var arr) || arr.ValueKind != JsonValueKind.Array)
            {
                return (null, null, null);
            }
            // pending.RegisteredAtUtc is our "look back from here" anchor.
            // Microsoft's transcripts API doesn't filter by createdDateTime
            // server-side reliably, so we filter client-side: only consider
            // transcripts created after our register time minus a 24h
            // grace window (handles transcripts that landed before the
            // fetcher was registered — see DebugController which defaults
            // RegisteredAtUtc to 24h back for that reason).
            var minCreated = pending.RegisteredAtUtc.UtcDateTime.AddHours(-1);
            foreach (var item in arr.EnumerateArray())
            {
                var transcriptId = item.TryGetProperty("id", out var i) ? i.GetString() : null;
                var createdAt = item.TryGetProperty("createdDateTime", out var c) ? c.GetString() : null;
                if (string.IsNullOrEmpty(transcriptId)) continue;
                if (!string.IsNullOrEmpty(createdAt)
                    && DateTimeOffset.TryParse(createdAt, CultureInfo.InvariantCulture,
                        DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
                        out var parsed)
                    && parsed.UtcDateTime < minCreated)
                {
                    continue;
                }
                return (pending.BotCallId, transcriptId, createdAt);
            }
        }
        catch (GraphApiException ex) when (ex.StatusCode is HttpStatusCode.NotFound or HttpStatusCode.Forbidden)
        {
            _logger.LogDebug(
                "List transcripts returned {Status} for organizer={Oid} meeting={Mid}; polling will retry.",
                ex.StatusCode, pending.OrganizerOid, pending.BotCallId);
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex,
                "List transcripts probe failed for organizer={Oid} meeting={Mid}; polling will retry.",
                pending.OrganizerOid, pending.BotCallId);
        }

        return (null, null, null);
    }

    private async Task<string?> FetchVttAsync(
        string meetingId,
        string transcriptId,
        CancellationToken cancellationToken)
    {
        // The list call returned a meetingId equal to pending.BotCallId
        // and was scoped to pending.OrganizerOid; reuse those for the
        // content fetch URL. Caller passes meetingId verbatim from
        // TryFindTranscriptAsync; we need the organizer too, so look it
        // up from the active fetch table.
        var organizerOid = _activeFetchOrganizers.GetValueOrDefault(meetingId);
        if (string.IsNullOrWhiteSpace(organizerOid))
        {
            _logger.LogWarning(
                "FetchVttAsync: no organizer cached for meetingId={MeetingId}; cannot fetch.",
                meetingId);
            return null;
        }

        var canonicalMeetingId = ToCanonicalMeetingId(meetingId);
        var resource =
            $"https://graph.microsoft.com/v1.0/users/{Uri.EscapeDataString(organizerOid)}" +
            $"/onlineMeetings/{Uri.EscapeDataString(canonicalMeetingId)}/transcripts/{Uri.EscapeDataString(transcriptId)}/content" +
            "?$format=text/vtt&useResourceSpecificConsentBasedAuthorization=true";
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

    /// <summary>
    /// Convert a chat-thread-shaped meeting id
    /// (<c>19:meeting_xxx@thread.v2</c>) into Microsoft Graph's canonical
    /// <c>onlineMeeting.id</c> — URL-safe base64 of
    /// <c>1*{tenantId}*0**{chatThreadId}</c>. Already-canonical ids
    /// (no leading <c>19:</c>) pass through unchanged.
    /// </summary>
    private string ToCanonicalMeetingId(string meetingId)
    {
        if (string.IsNullOrWhiteSpace(meetingId)) return meetingId;
        if (!meetingId.StartsWith("19:", StringComparison.Ordinal)) return meetingId;
        var tenantId = _botConfig.TenantId ?? string.Empty;
        if (string.IsNullOrWhiteSpace(tenantId)) return meetingId;
        var raw = $"1*{tenantId}*0**{meetingId}";
        var bytes = System.Text.Encoding.UTF8.GetBytes(raw);
        var b64 = Convert.ToBase64String(bytes)
            .TrimEnd('=')
            .Replace('+', '-')
            .Replace('/', '_');
        return b64;
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
        var fetchedAt = DateTimeOffset.UtcNow.ToString("O");
        var payload = new MeetingOfficialTranscriptPayload
        {
            TranscriptId = transcriptId,
            OrganizerOid = pending.OrganizerOid,
            FetchedAtUtc = fetchedAt,
            CreatedAtUtc = createdAt,
            VttUrl = $"meetings/{SanitizeBlobSegment(meetingId)}/transcripts/official.vtt",
            CueCount = cues.Count,
            Cues = cues,
        };

        await _dispatcher.PublishAsync(new AlfredEventEnvelope
        {
            EventType = AlfredEventTypes.MeetingTranscriptOfficial,
            EventId = Guid.NewGuid().ToString("N"),
            Ts = createdAt ?? fetchedAt,
            MeetingRef = new MeetingRef
            {
                MeetingId = meetingId,
                MeetingChatThreadId = string.IsNullOrWhiteSpace(pending.MeetingChatThreadId)
                    ? null : pending.MeetingChatThreadId,
            },
            Payload = payload,
        }, cancellationToken).ConfigureAwait(false);

        if (_blobArchive is { IsEnabled: true })
        {
            _ = _blobArchive.ArchiveOfficialTranscriptAsync(meetingId, vtt, cancellationToken);
        }

        _logger.LogInformation(
            "Emitted meeting.transcript.official meetingId={MeetingId} transcriptId={TranscriptId} cues={CueCount} (botCallId={CallId})",
            meetingId, transcriptId, cues.Count, pending.BotCallId);
    }

    private static IReadOnlyList<OfficialTranscriptCue> ParseVtt(string vtt)
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
                Speaker = speaker is null ? null : new SpeakerRef { DisplayName = speaker },
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

    private static string SanitizeBlobSegment(string s) =>
        Regex.Replace(s, @"[^a-zA-Z0-9\-_.]", "_");

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
        string MeetingChatThreadId,
        DateTimeOffset RegisteredAtUtc);
}
