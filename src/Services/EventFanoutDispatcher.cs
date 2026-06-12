using System.Collections.Concurrent;
using System.Net.Http.Json;
using System.Text.Json;
using System.Threading.Channels;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Marker name used for the synthetic fallback consumer derived from
/// <see cref="EventDispatchConfiguration.BootstrapConsumerUrl"/>. Used
/// when an event has no matching channel attachment (typically
/// per-meeting / group-chat installs) so events still reach the sink.
/// </summary>
file static class FallbackConsumerName
{
    public const string Value = "fallback-default";
}

/// <summary>
/// Resolves the per-channel <see cref="ConsumerConfig"/> list for an
/// outbound event and POSTs a versioned <see cref="AlfredEventEnvelope"/>
/// to each enabled consumer. Owns the per-consumer queue and background
/// drain task; <see cref="PublishAsync"/> never blocks on consumer
/// latency.
///
/// <para>
/// This is the single sanctioned outbound path. The bot does not know
/// which downstream systems exist; it just publishes envelopes to
/// every URL registered for a channel and lets each consumer interpret
/// them. See <c>docs/event-contract.md</c> for the wire shape.
/// </para>
/// </summary>
public sealed class EventFanoutDispatcher : IAsyncDisposable
{
    private static readonly JsonSerializerOptions SerializerOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull,
    };

    private const int QueueCapacity = 1000;
    private const int MaxAttempts = 3;
    private static readonly TimeSpan[] RetryDelays =
    [
        TimeSpan.FromMilliseconds(250),
        TimeSpan.FromSeconds(1),
        TimeSpan.FromSeconds(4),
    ];

    private readonly ChannelAttachmentStore _store;
    private readonly MeetingChannelLinkStore? _meetingLinks;
    private readonly ClientRouteStore? _clientRoutes;
    private readonly ClientBlobMirror? _clientBlobMirror;
    private readonly IHttpClientFactory _httpClientFactory;
    private readonly ILogger<EventFanoutDispatcher> _logger;
    private readonly ILoggerFactory _loggerFactory;
    private readonly MeetingAuditLogger? _auditLogger;
    private readonly BlobEventArchive? _blobArchive;
    private readonly IReadOnlyList<ConsumerConfig> _fallbackConsumers;
    private readonly TimeSpan _partialThrottle;
    private readonly ConcurrentDictionary<string, DateTimeOffset> _lastPartialEmitted = new(StringComparer.Ordinal);
    private readonly CancellationTokenSource _cts = new();
    private readonly ConcurrentDictionary<string, ConsumerWorker> _workers = new(StringComparer.Ordinal);
    private bool _disposed;

    public EventFanoutDispatcher(
        ChannelAttachmentStore store,
        IHttpClientFactory httpClientFactory,
        EventDispatchConfiguration dispatchConfig,
        ILogger<EventFanoutDispatcher> logger,
        ILoggerFactory loggerFactory,
        MeetingAuditLogger? auditLogger = null,
        BlobEventArchive? blobArchive = null,
        MeetingChannelLinkStore? meetingLinks = null,
        ClientRouteStore? clientRoutes = null,
        ClientBlobMirror? clientBlobMirror = null)
    {
        _store = store ?? throw new ArgumentNullException(nameof(store));
        _httpClientFactory = httpClientFactory ?? throw new ArgumentNullException(nameof(httpClientFactory));
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
        _loggerFactory = loggerFactory ?? throw new ArgumentNullException(nameof(loggerFactory));
        _auditLogger = auditLogger;
        _blobArchive = blobArchive;
        _meetingLinks = meetingLinks;
        _clientRoutes = clientRoutes;
        _clientBlobMirror = clientBlobMirror;

        var throttleSeconds = Math.Max(0, dispatchConfig?.PartialThrottleSeconds ?? 60);
        _partialThrottle = TimeSpan.FromSeconds(throttleSeconds);

        // Fallback consumer for events that don't match any channel
        // attachment (per-meeting installs, group chats). Without this,
        // a bot that joins a meeting via /api/calling/join records
        // audit to disk but never reaches any sink. With it set, events
        // route to the bootstrap URL by default; channel attachments
        // override on a per-channel basis.
        var bootstrapUrl = dispatchConfig?.BootstrapConsumerUrl;
        _fallbackConsumers = !string.IsNullOrWhiteSpace(bootstrapUrl)
            ? new[]
              {
                  new ConsumerConfig
                  {
                      Name = FallbackConsumerName.Value,
                      Url = bootstrapUrl!,
                      EventKinds = new[] { "*" },
                      Enabled = true,
                  },
              }
            : Array.Empty<ConsumerConfig>();
    }

    /// <summary>
    /// Hands an envelope off to every enabled consumer registered for the
    /// envelope's <c>(team_id, channel_id)</c>, or for the channel
    /// attachment whose <c>conversation_thread_id</c> matches the
    /// envelope's <c>chat_thread_id</c>. Non-blocking — audits to disk
    /// inline (cheap append) then drops to per-consumer bounded queues.
    /// </summary>
    public ValueTask PublishAsync(AlfredEventEnvelope envelope, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(envelope);
        ObjectDisposedException.ThrowIf(_disposed, this);

        // If a meeting has been linked to a channel via the `@Alfred link to <channel>`
        // command, stamp the ChannelLink onto MeetingRef BEFORE audit + blob + fan-out
        // see it, so blob paths and consumer routing use the linked channel.
        envelope = StampWithMeetingLinkIfApplicable(envelope);

        var auditKey = envelope.MeetingRef?.MeetingId
            ?? (envelope.ChannelRef is not null
                ? $"{envelope.ChannelRef.TeamId}|{envelope.ChannelRef.ChannelId}"
                : null);
        if (_auditLogger is not null && auditKey is not null)
        {
            _auditLogger.Append(auditKey, AuditFolderFor(envelope.EventType), envelope);
        }

        // Partial-transcript throttle: STT emits interim hypotheses every
        // ~250ms. Without this, a 30-min meeting fans out thousands of
        // partial POSTs + blob files per speaker. Throttle keeps the
        // local NDJSON audit (cheap append) intact so the /debug tail
        // still shows everything, but suppresses the expensive blob +
        // consumer paths for partials within the window. Finals
        // (transcript.final) are never throttled.
        if (IsThrottledPartial(envelope))
        {
            return ValueTask.CompletedTask;
        }

        // Blob archive is fire-and-forget so it cannot slow the hot
        // dispatch path. ArchiveEnvelopeAsync internally swallows errors.
        if (_blobArchive is { IsEnabled: true })
        {
            _ = _blobArchive.ArchiveEnvelopeAsync(envelope, _cts.Token);
        }

        // Email-based client routing (PLAN.md): a meeting bound to a
        // registered client route wins over channel consumers and the
        // bootstrap fallback. The client's optional storage container
        // mirror is unfiltered (it is their archive); event_kinds only
        // filters the HTTP push below.
        var clientRoute = ResolveClientRoute(envelope);
        if (clientRoute is not null && _clientBlobMirror is not null
            && !string.IsNullOrWhiteSpace(clientRoute.StorageContainerUrl))
        {
            _ = _clientBlobMirror.MirrorAsync(clientRoute, envelope, _cts.Token);
        }

        var consumers = clientRoute is not null
            ? new[]
              {
                  new ConsumerConfig
                  {
                      Name = $"client:{clientRoute.Email}",
                      Url = clientRoute.SinkUrl,
                      EventKinds = clientRoute.EventKinds,
                      Headers = clientRoute.Headers,
                      Enabled = true,
                  },
              }
            : ResolveConsumers(envelope);
        if (consumers.Count == 0)
        {
            return ValueTask.CompletedTask;
        }

        foreach (var consumer in consumers)
        {
            if (!consumer.Enabled)
            {
                continue;
            }

            if (!MatchesEventKind(consumer.EventKinds, envelope.EventType))
            {
                continue;
            }

            var worker = _workers.GetOrAdd(
                consumer.Url,
                static (_, ctx) => new ConsumerWorker(
                    ctx.url,
                    ctx.factory.CreateClient(),
                    ctx.factory,
                    ctx.loggerFactory.CreateLogger<ConsumerWorker>(),
                    ctx.cts.Token),
                (url: consumer.Url, factory: _httpClientFactory, loggerFactory: _loggerFactory, cts: _cts));

            worker.Enqueue(envelope, consumer.Headers);
        }

        return ValueTask.CompletedTask;
    }

    private static string AuditFolderFor(string eventType) => eventType switch
    {
        AlfredEventTypes.MeetingTranscriptPartial or AlfredEventTypes.MeetingTranscriptFinal => "transcript",
        AlfredEventTypes.ChannelMessageCreated or AlfredEventTypes.ChannelMessageUpdated or
        AlfredEventTypes.ChannelMessageDeleted or AlfredEventTypes.MeetingChatCreated or
        AlfredEventTypes.MeetingChatUpdated or AlfredEventTypes.MeetingChatDeleted => "chat",
        _ => "system",
    };

    private AlfredEventEnvelope StampWithMeetingLinkIfApplicable(AlfredEventEnvelope envelope)
    {
        if (_meetingLinks is null) return envelope;
        if (envelope.MeetingRef is null) return envelope;
        // If a channel link is already present the event is already stamped.
        if (envelope.MeetingRef.ChannelLink is not null) return envelope;
        var link = _meetingLinks.GetChannelLink(envelope.MeetingRef.MeetingId);
        if (link is null) return envelope;
        return envelope with
        {
            MeetingRef = envelope.MeetingRef with { ChannelLink = link },
        };
    }

    /// <summary>
    /// True iff this is a <c>meeting.transcript.partial</c> for a
    /// <c>(meeting_id, speaker_id)</c> we've already emitted within the
    /// configured throttle window. Throttle of 0 disables — every partial passes.
    /// </summary>
    private bool IsThrottledPartial(AlfredEventEnvelope envelope)
    {
        if (_partialThrottle <= TimeSpan.Zero) return false;
        if (!string.Equals(envelope.EventType, AlfredEventTypes.MeetingTranscriptPartial, StringComparison.Ordinal))
        {
            return false;
        }

        var speakerId = (envelope.Payload as MeetingTranscriptPayload)?.Speaker?.Id ?? "_unknown";
        var meetingId = envelope.MeetingRef?.MeetingId ?? "_unknown";
        var key = $"{meetingId}|{speakerId}";
        var now = DateTimeOffset.UtcNow;
        var last = _lastPartialEmitted.GetValueOrDefault(key, DateTimeOffset.MinValue);
        if (now - last < _partialThrottle)
        {
            return true;
        }
        _lastPartialEmitted[key] = now;
        return false;
    }

    /// <summary>
    /// Returns the enabled client route this meeting event is bound to,
    /// or null. Bindings key on the meeting chat thread id; the meeting
    /// id is tried second because it equals the thread id whenever
    /// canonical resolution hasn't happened yet.
    /// </summary>
    private ClientRouteRecord? ResolveClientRoute(AlfredEventEnvelope envelope)
    {
        if (_clientRoutes is null || envelope.MeetingRef is not { } mr)
        {
            return null;
        }
        return _clientRoutes.RouteForMeeting(mr.MeetingChatThreadId)
            ?? _clientRoutes.RouteForMeeting(mr.MeetingId);
    }

    private IReadOnlyList<ConsumerConfig> ResolveConsumers(AlfredEventEnvelope envelope)
    {
        ChannelAttachmentRecord? record = null;

        // Channel events: look up by team + channel directly.
        if (envelope.ChannelRef is { } cr)
        {
            record = _store.Get(cr.TeamId, cr.ChannelId);
        }

        // Meeting events: if linked to a channel, look up by that channel.
        if (record is null && envelope.MeetingRef?.ChannelLink is { } link)
        {
            record = _store.Get(link.TeamId, link.ChannelId);
        }

        // Fallback: look up by meeting chat thread id as a conversation id.
        if (record is null && !string.IsNullOrWhiteSpace(envelope.MeetingRef?.MeetingChatThreadId))
        {
            record = _store.GetByConversationThreadId(envelope.MeetingRef.MeetingChatThreadId!);
        }

        if (record is not null && record.Consumers.Count > 0)
        {
            return record.Consumers;
        }

        return _fallbackConsumers;
    }

    private static bool MatchesEventKind(IReadOnlyList<string> kinds, string eventType)
    {
        if (kinds.Count == 0)
        {
            return true;
        }
        for (var i = 0; i < kinds.Count; i++)
        {
            var k = kinds[i];
            if (k == "*" || string.Equals(k, eventType, StringComparison.Ordinal))
            {
                return true;
            }
        }
        return false;
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        _cts.Cancel();
        var workers = _workers.Values.ToList();
        foreach (var w in workers)
        {
            await w.DisposeAsync();
        }
        _cts.Dispose();
    }

    /// <summary>
    /// Per-consumer-URL drain loop. One bounded channel + one background
    /// task. Drop-oldest on overflow so a slow consumer can never block
    /// the producer; bounded retry so a flapping consumer logs once and
    /// moves on.
    /// </summary>
    private sealed class ConsumerWorker : IAsyncDisposable
    {
        private readonly string _url;
        private readonly HttpClient _httpClient;
        private readonly ILogger<ConsumerWorker> _logger;
        private readonly Channel<QueuedEnvelope> _channel;
        private readonly Task _drainTask;
        private readonly CancellationToken _shutdownToken;

        internal ConsumerWorker(
            string url,
            HttpClient httpClient,
            IHttpClientFactory _,
            ILogger<ConsumerWorker> logger,
            CancellationToken shutdownToken)
        {
            _url = url;
            _httpClient = httpClient;
            _httpClient.Timeout = TimeSpan.FromSeconds(10);
            _logger = logger;
            _shutdownToken = shutdownToken;
            _channel = Channel.CreateBounded<QueuedEnvelope>(new BoundedChannelOptions(QueueCapacity)
            {
                FullMode = BoundedChannelFullMode.DropOldest,
                SingleReader = true,
                SingleWriter = false,
            });
            _drainTask = Task.Run(DrainAsync);
        }

        internal void Enqueue(AlfredEventEnvelope envelope, IReadOnlyDictionary<string, string>? headers)
        {
            // Bounded channel with DropOldest never returns false on TryWrite;
            // if it ever did we would log so the operator can see overflow.
            if (!_channel.Writer.TryWrite(new QueuedEnvelope(envelope, headers)))
            {
                _logger.LogWarning("Consumer {Url} queue rejected an event; dropping.", _url);
            }
        }

        private async Task DrainAsync()
        {
            try
            {
                await foreach (var item in _channel.Reader.ReadAllAsync(_shutdownToken))
                {
                    await PostWithRetryAsync(item).ConfigureAwait(false);
                }
            }
            catch (OperationCanceledException)
            {
                // shutdown
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Consumer drain task for {Url} crashed", _url);
            }
        }

        private async Task PostWithRetryAsync(QueuedEnvelope item)
        {
            for (var attempt = 0; attempt < MaxAttempts; attempt++)
            {
                try
                {
                    using var request = new HttpRequestMessage(HttpMethod.Post, _url)
                    {
                        Content = JsonContent.Create(item.Envelope, options: SerializerOptions),
                    };
                    if (item.Headers is not null)
                    {
                        foreach (var (k, v) in item.Headers)
                        {
                            request.Headers.TryAddWithoutValidation(k, v);
                        }
                    }

                    using var response = await _httpClient
                        .SendAsync(request, HttpCompletionOption.ResponseHeadersRead, _shutdownToken)
                        .ConfigureAwait(false);

                    if (response.IsSuccessStatusCode)
                    {
                        return;
                    }

                    if ((int)response.StatusCode is >= 400 and < 500
                        and not 408 and not 429)
                    {
                        _logger.LogWarning(
                            "Consumer {Url} returned {Status} for event {EventType} ({EventId}); not retrying.",
                            _url, (int)response.StatusCode, item.Envelope.EventType, item.Envelope.EventId);
                        return;
                    }

                    _logger.LogInformation(
                        "Consumer {Url} returned {Status} for {EventType} (attempt {Attempt}/{Max}); will retry.",
                        _url, (int)response.StatusCode, item.Envelope.EventType, attempt + 1, MaxAttempts);
                }
                catch (OperationCanceledException) when (_shutdownToken.IsCancellationRequested)
                {
                    return;
                }
                catch (Exception ex)
                {
                    _logger.LogInformation(
                        ex,
                        "Consumer {Url} POST failed for {EventType} (attempt {Attempt}/{Max}); will retry if attempts remain.",
                        _url, item.Envelope.EventType, attempt + 1, MaxAttempts);
                }

                if (attempt + 1 < MaxAttempts)
                {
                    try
                    {
                        await Task.Delay(RetryDelays[attempt], _shutdownToken).ConfigureAwait(false);
                    }
                    catch (OperationCanceledException)
                    {
                        return;
                    }
                }
            }

            _logger.LogWarning(
                "Consumer {Url} dropped event {EventType} ({EventId}) after {Max} attempts.",
                _url, item.Envelope.EventType, item.Envelope.EventId, MaxAttempts);
        }

        public async ValueTask DisposeAsync()
        {
            _channel.Writer.TryComplete();
            try
            {
                await _drainTask.ConfigureAwait(false);
            }
            catch
            {
                // best-effort
            }
            _httpClient.Dispose();
        }

        private readonly record struct QueuedEnvelope(
            AlfredEventEnvelope Envelope,
            IReadOnlyDictionary<string, string>? Headers);
    }
}
