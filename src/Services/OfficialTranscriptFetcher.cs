using System.Collections.Concurrent;
using System.Globalization;
using System.Net;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Configuration for <see cref="OfficialTranscriptFetcher"/>.
/// </summary>
public sealed class OfficialTranscriptFetcherOptions
{
    /// <summary>
    /// Absolute path to the JSON file that persists pending
    /// transcript-fetch sessions across bot restarts. Without it, a
    /// redeploy mid-poll silently drops every in-flight fetcher.
    /// </summary>
    public required string FilePath { get; init; }
}

/// <summary>
/// On-disk representation of one pending fetch session. Kept distinct
/// from the in-memory <see cref="OfficialTranscriptFetcher.PollSession"/>
/// so the persistence schema can evolve independently.
/// </summary>
public sealed record PendingTranscriptFetchRecord
{
    [JsonPropertyName("bot_call_id")] public required string BotCallId { get; init; }
    [JsonPropertyName("organizer_oid")] public required string OrganizerOid { get; init; }
    [JsonPropertyName("meeting_chat_thread_id")] public string? MeetingChatThreadId { get; init; }
    [JsonPropertyName("registered_at_utc")] public required DateTimeOffset RegisteredAtUtc { get; init; }
    [JsonPropertyName("deadline_utc")] public required DateTimeOffset DeadlineUtc { get; init; }
    [JsonPropertyName("retry_used")] public bool RetryUsed { get; init; }
}

/// <summary>
/// After Alfred sees a meeting (chat sighting or end activity),
/// schedules a background poll for the post-meeting Microsoft
/// transcript and, when it lands, publishes a
/// <see cref="AlfredEventTypes.MeetingTranscriptOfficial"/> envelope
/// through the fan-out dispatcher.
///
/// <para>
/// We poll the <b>user-scoped</b> Graph resource
/// <c>users/{organizer}/onlineMeetings/{meeting}/transcripts?useResourceSpecificConsentBasedAuthorization=true</c>
/// every 60s starting 60s after Register. The RSC flag makes Graph
/// evaluate <c>OnlineMeetingTranscript.Read.Chat</c> (consented at
/// "+Apps" install) instead of demanding the tenant-wide equivalent.
/// </para>
///
/// <para>
/// Reliability behaviour:
/// </para>
/// <list type="bullet">
///   <item>
///     Per-meeting state is a <see cref="PollSession"/> with a mutable
///     deadline. Repeat <see cref="Register"/> calls for the same
///     <c>botCallId</c> extend the deadline — a start-time register
///     (first chat sighting) and an end-time register
///     (<c>OnTeamsMeetingEndAsync</c>) cooperate so the poll window is
///     anchored on the most recent signal, regardless of meeting
///     length.
///   </item>
///   <item>
///     Sessions are persisted to a JSON file on every state change.
///     Bot restart resumes every pending poll with its on-disk
///     deadline; nothing is silently lost across redeploys.
///   </item>
///   <item>
///     If the first 30-min poll budget elapses without a transcript,
///     the session sleeps one hour and runs one more 30-min window.
///     After that, give up and rely on operator backfill via
///     <c>POST /api/debug/fetch-transcript</c>.
///   </item>
///   <item>
///     Channel-meeting chat thread ids (the <c>@thread.tacv2</c>
///     suffix) are skipped at registration. Microsoft does not expose
///     channel-meeting transcripts through any public Graph endpoint
///     this bot can call (README §7.2); polling them only burns the
///     30-min budget on guaranteed 404s.
///   </item>
/// </list>
/// </summary>
public sealed partial class OfficialTranscriptFetcher : IHostedService, IAsyncDisposable
{
    private static readonly TimeSpan InitialDelay = TimeSpan.FromSeconds(60);
    private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(60);
    private static readonly TimeSpan PollDuration = TimeSpan.FromMinutes(30);
    private static readonly TimeSpan RetryGap = TimeSpan.FromHours(1);

    private static readonly JsonSerializerOptions PersistenceOptions = new(JsonSerializerDefaults.Web)
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly EventFanoutDispatcher _dispatcher;
    private readonly GraphApiClient _graph;
    private readonly BotConfiguration _botConfig;
    private readonly BlobEventArchive? _blobArchive;
    private readonly OfficialTranscriptFetcherOptions _options;
    private readonly ILogger<OfficialTranscriptFetcher> _logger;
    private readonly CancellationTokenSource _cts = new();
    private readonly ConcurrentDictionary<string, PollSession> _sessions = new(StringComparer.Ordinal);
    private readonly SemaphoreSlim _persistMutex = new(1, 1);
    private bool _disposed;

    public OfficialTranscriptFetcher(
        EventFanoutDispatcher dispatcher,
        GraphApiClient graph,
        BotConfiguration botConfig,
        OfficialTranscriptFetcherOptions options,
        ILogger<OfficialTranscriptFetcher> logger,
        BlobEventArchive? blobArchive = null)
    {
        _dispatcher = dispatcher;
        _graph = graph;
        _botConfig = botConfig;
        _blobArchive = blobArchive;
        _options = options ?? throw new ArgumentNullException(nameof(options));
        _logger = logger;
    }

    public Task StartAsync(CancellationToken cancellationToken) => LoadFromDiskAsync(cancellationToken);

    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;

    /// <summary>
    /// Schedule a post-meeting fetch, or extend an existing session's
    /// deadline. The most recent caller wins on deadline (so end-time
    /// registers anchor the window to "30 min after meeting end"
    /// regardless of how long the meeting ran); the earliest caller
    /// wins on <see cref="PollSession.RegisteredAtUtc"/> (so the
    /// createdDateTime filter in <see cref="TryFindTranscriptAsync"/>
    /// doesn't tighten on re-entry).
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

        // Channel meetings have no public Graph transcripts endpoint
        // (README §7.2 — `OnlineMeetingTranscript.Read.Chat` is private
        // chat only; `ChannelMeetingTranscript.Read.Group` has no
        // public GET). Skip them so we don't burn a 30-min poll budget
        // on guaranteed 404s.
        if (LooksLikeChannelThreadId(meetingChatThreadId) || LooksLikeChannelThreadId(botCallId))
        {
            _logger.LogInformation(
                "Skipping transcript fetcher for channel meeting (no public Graph transcripts endpoint). botCallId={CallId} meetingChatThreadId={MeetingChatThreadId}",
                botCallId, meetingChatThreadId);
            return;
        }

        var fresh = new PollSession
        {
            BotCallId = botCallId,
            OrganizerOid = organizerOid,
            MeetingChatThreadId = meetingChatThreadId,
            RegisteredAtUtc = registeredAtUtc,
            Deadline = DateTimeOffset.UtcNow + PollDuration,
        };

        var session = _sessions.GetOrAdd(botCallId, fresh);
        if (ReferenceEquals(session, fresh))
        {
            session.Task = Task.Run(() => RunAsync(session, _cts.Token));
            _logger.LogInformation(
                "Scheduled transcript fetch botCallId={CallId} organizerOid={Oid} deadline={Deadline:O}",
                botCallId, organizerOid, session.Deadline);
        }
        else
        {
            lock (session.SyncRoot)
            {
                var pushed = DateTimeOffset.UtcNow + PollDuration;
                if (pushed > session.Deadline) session.Deadline = pushed;
                if (registeredAtUtc < session.RegisteredAtUtc) session.RegisteredAtUtc = registeredAtUtc;
                if (string.IsNullOrWhiteSpace(session.OrganizerOid)) session.OrganizerOid = organizerOid;
                if (string.IsNullOrWhiteSpace(session.MeetingChatThreadId)) session.MeetingChatThreadId = meetingChatThreadId;
            }
            _logger.LogInformation(
                "Extended transcript fetch botCallId={CallId} new_deadline={Deadline:O}",
                botCallId, session.Deadline);
        }

        FireAndForgetPersist();
    }

    private async Task RunAsync(PollSession session, CancellationToken cancellationToken)
    {
        try
        {
            await Task.Delay(InitialDelay, cancellationToken).ConfigureAwait(false);

            while (true)
            {
                cancellationToken.ThrowIfCancellationRequested();

                DateTimeOffset deadline;
                bool retryUsed;
                lock (session.SyncRoot)
                {
                    deadline = session.Deadline;
                    retryUsed = session.RetryUsed;
                }

                if (DateTimeOffset.UtcNow >= deadline)
                {
                    if (retryUsed)
                    {
                        _logger.LogWarning(
                            "Official transcript fetch exhausted retry for botCallId={CallId} meetingChatThreadId={MeetingChatThreadId}; operator can backfill via /api/debug/fetch-transcript.",
                            session.BotCallId, session.MeetingChatThreadId);
                        return;
                    }

                    _logger.LogInformation(
                        "Official transcript fetch hit first deadline botCallId={CallId}; sleeping {RetryGap} before one more {PollDuration} window.",
                        session.BotCallId, RetryGap, PollDuration);

                    try
                    {
                        await Task.Delay(RetryGap, cancellationToken).ConfigureAwait(false);
                    }
                    catch (OperationCanceledException)
                    {
                        return;
                    }

                    lock (session.SyncRoot)
                    {
                        session.RetryUsed = true;
                        var pushed = DateTimeOffset.UtcNow + PollDuration;
                        if (pushed > session.Deadline) session.Deadline = pushed;
                    }
                    FireAndForgetPersist();
                    continue;
                }

                var (meetingId, transcriptId, createdAt) =
                    await TryFindTranscriptAsync(session, cancellationToken).ConfigureAwait(false);

                if (!string.IsNullOrEmpty(meetingId) && !string.IsNullOrEmpty(transcriptId))
                {
                    var vtt = await FetchVttAsync(session, meetingId!, transcriptId!, cancellationToken)
                        .ConfigureAwait(false);
                    if (!string.IsNullOrWhiteSpace(vtt))
                    {
                        await EmitAsync(session, meetingId!, transcriptId!, createdAt, vtt, cancellationToken)
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
        }
        catch (OperationCanceledException) { /* shutdown */ }
        catch (Exception ex)
        {
            _logger.LogError(ex,
                "Official transcript fetch crashed botCallId={CallId} meetingChatThreadId={MeetingChatThreadId}.",
                session.BotCallId, session.MeetingChatThreadId);
        }
        finally
        {
            _sessions.TryRemove(session.BotCallId, out _);
            FireAndForgetPersist();
        }
    }

    private async Task<(string? MeetingId, string? TranscriptId, string? CreatedAt)>
        TryFindTranscriptAsync(PollSession session, CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(session.OrganizerOid)
            || string.IsNullOrWhiteSpace(session.BotCallId))
        {
            return (null, null, null);
        }

        var canonicalMeetingId = ToCanonicalMeetingId(session.BotCallId);
        // NOTE: Graph's per-meeting transcripts endpoint REJECTS $orderby
        // and $top with `400 Query option 'OrderBy' is not allowed`.
        // List everything; pick newest in-process below.
        var resource =
            $"https://graph.microsoft.com/v1.0/users/{Uri.EscapeDataString(session.OrganizerOid!)}" +
            $"/onlineMeetings/{Uri.EscapeDataString(canonicalMeetingId)}/transcripts" +
            "?useResourceSpecificConsentBasedAuthorization=true";

        try
        {
            using var doc = await _graph.GetResourceAsync(resource, cancellationToken).ConfigureAwait(false);
            if (!doc.RootElement.TryGetProperty("value", out var arr) || arr.ValueKind != JsonValueKind.Array)
            {
                return (null, null, null);
            }
            // RegisteredAtUtc - 1h is our "look back from here" anchor.
            // Microsoft's transcripts API doesn't filter by createdDateTime
            // server-side reliably, so we filter client-side: only consider
            // transcripts created after our register time minus 1h.
            var minCreated = session.RegisteredAtUtc.UtcDateTime.AddHours(-1);
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
                return (session.BotCallId, transcriptId, createdAt);
            }
        }
        catch (GraphApiException ex) when (ex.StatusCode is HttpStatusCode.NotFound or HttpStatusCode.Forbidden)
        {
            _logger.LogDebug(
                "List transcripts returned {Status} for organizer={Oid} meeting={Mid}; polling will retry.",
                ex.StatusCode, session.OrganizerOid, session.BotCallId);
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex,
                "List transcripts probe failed for organizer={Oid} meeting={Mid}; polling will retry.",
                session.OrganizerOid, session.BotCallId);
        }

        return (null, null, null);
    }

    private async Task<string?> FetchVttAsync(
        PollSession session,
        string meetingId,
        string transcriptId,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(session.OrganizerOid))
        {
            _logger.LogWarning(
                "FetchVttAsync: no organizer on session for meetingId={MeetingId}; cannot fetch.",
                meetingId);
            return null;
        }

        var canonicalMeetingId = ToCanonicalMeetingId(meetingId);
        var resource =
            $"https://graph.microsoft.com/v1.0/users/{Uri.EscapeDataString(session.OrganizerOid!)}" +
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
        PollSession session,
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
            OrganizerOid = session.OrganizerOid,
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
                MeetingChatThreadId = string.IsNullOrWhiteSpace(session.MeetingChatThreadId)
                    ? null : session.MeetingChatThreadId,
            },
            Payload = payload,
        }, cancellationToken).ConfigureAwait(false);

        if (_blobArchive is { IsEnabled: true })
        {
            _ = _blobArchive.ArchiveOfficialTranscriptAsync(meetingId, vtt, cancellationToken);
        }

        _logger.LogInformation(
            "Emitted meeting.transcript.official meetingId={MeetingId} transcriptId={TranscriptId} cues={CueCount} (botCallId={CallId})",
            meetingId, transcriptId, cues.Count, session.BotCallId);
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

    private static bool LooksLikeChannelThreadId(string? id) =>
        !string.IsNullOrWhiteSpace(id)
        && id!.StartsWith("19:", StringComparison.Ordinal)
        && id.IndexOf("@thread.tacv2", StringComparison.Ordinal) > 0;

    private void FireAndForgetPersist()
    {
        // Mid-shutdown Persist calls are best-effort. The temp+rename
        // pattern means a cancelled write never leaves a corrupt file.
        _ = Task.Run(() => PersistAsync(_cts.Token));
    }

    private async Task PersistAsync(CancellationToken cancellationToken)
    {
        try
        {
            await _persistMutex.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                var snapshot = _sessions.Values.Select(s =>
                {
                    lock (s.SyncRoot)
                    {
                        return new PendingTranscriptFetchRecord
                        {
                            BotCallId = s.BotCallId,
                            OrganizerOid = s.OrganizerOid ?? string.Empty,
                            MeetingChatThreadId = s.MeetingChatThreadId,
                            RegisteredAtUtc = s.RegisteredAtUtc,
                            DeadlineUtc = s.Deadline,
                            RetryUsed = s.RetryUsed,
                        };
                    }
                }).ToList();

                Directory.CreateDirectory(Path.GetDirectoryName(_options.FilePath)!);
                var tmp = _options.FilePath + ".tmp";
                await using (var stream = File.Create(tmp))
                {
                    await JsonSerializer.SerializeAsync(stream, snapshot, PersistenceOptions, cancellationToken).ConfigureAwait(false);
                }
                File.Move(tmp, _options.FilePath, overwrite: true);
            }
            finally
            {
                _persistMutex.Release();
            }
        }
        catch (OperationCanceledException) { }
        catch (Exception ex)
        {
            _logger.LogWarning(ex,
                "Failed to persist pending-transcript-fetch store at {FilePath}; in-memory state retained.",
                _options.FilePath);
        }
    }

    /// <summary>
    /// Load pending fetches from disk and resume them. Records whose
    /// retry has already been used AND whose deadline has passed are
    /// dropped — there is no more work to do for them.
    /// </summary>
    public async Task LoadFromDiskAsync(CancellationToken cancellationToken)
    {
        if (!File.Exists(_options.FilePath))
        {
            _logger.LogInformation(
                "Pending-transcript-fetch store {FilePath} does not exist yet; starting empty.",
                _options.FilePath);
            return;
        }

        List<PendingTranscriptFetchRecord>? records;
        try
        {
            await using var stream = File.OpenRead(_options.FilePath);
            records = await JsonSerializer.DeserializeAsync<List<PendingTranscriptFetchRecord>>(
                stream, PersistenceOptions, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex,
                "Failed to read pending-transcript-fetch store {FilePath}; starting empty.",
                _options.FilePath);
            return;
        }

        if (records is null) return;

        var resumed = 0;
        var dropped = 0;
        foreach (var r in records)
        {
            if (string.IsNullOrWhiteSpace(r.BotCallId) || string.IsNullOrWhiteSpace(r.OrganizerOid))
            {
                dropped++;
                continue;
            }
            if (r.RetryUsed && r.DeadlineUtc < DateTimeOffset.UtcNow)
            {
                dropped++;
                continue;
            }

            var session = new PollSession
            {
                BotCallId = r.BotCallId,
                OrganizerOid = r.OrganizerOid,
                MeetingChatThreadId = r.MeetingChatThreadId,
                RegisteredAtUtc = r.RegisteredAtUtc,
                Deadline = r.DeadlineUtc,
                RetryUsed = r.RetryUsed,
            };

            if (_sessions.TryAdd(r.BotCallId, session))
            {
                session.Task = Task.Run(() => RunAsync(session, _cts.Token));
                resumed++;
            }
        }

        _logger.LogInformation(
            "Pending-transcript-fetch store loaded: resumed={Resumed} dropped={Dropped} from {FilePath}",
            resumed, dropped, _options.FilePath);

        // Flush the cleaned view (drops the discarded entries).
        FireAndForgetPersist();
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed) return;
        _disposed = true;
        _cts.Cancel();
        var tasks = _sessions.Values
            .Select(s => s.Task)
            .Where(t => t is not null)
            .Cast<Task>()
            .ToArray();
        foreach (var t in tasks)
        {
            try { await t.ConfigureAwait(false); } catch { /* shutdown */ }
        }
        _cts.Dispose();
        _persistMutex.Dispose();
    }

    /// <summary>
    /// Mutable per-meeting poll state. <see cref="Register"/> extends
    /// the deadline on this object in-place; <see cref="RunAsync"/>
    /// re-reads the deadline each iteration so concurrent extensions
    /// take effect immediately.
    /// </summary>
    private sealed class PollSession
    {
        public required string BotCallId { get; init; }
        public string? OrganizerOid { get; set; }
        public string? MeetingChatThreadId { get; set; }
        public DateTimeOffset RegisteredAtUtc { get; set; }
        public DateTimeOffset Deadline { get; set; }
        public bool RetryUsed { get; set; }
        public Task? Task { get; set; }
        public object SyncRoot { get; } = new object();
    }
}
