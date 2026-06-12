namespace TeamsMediaBot.Services;

/// <summary>
/// One person who might own a meeting's client route, in PLAN.md
/// priority order: the person who added Alfred to the meeting chat
/// ("installer"), the meeting organizer ("organizer"), the first
/// non-bot sender ("sender"). Email is filled by the caller when Bot
/// Framework already exposed it (TeamsInfo); otherwise the resolver
/// falls back to the alias table, then Graph.
/// </summary>
public sealed record ClientIdentityCandidate
{
    public string? AadObjectId { get; init; }
    public string? Email { get; init; }
    public string? DisplayName { get; init; }
    public required string Source { get; init; }
}

/// <summary>
/// Binds meeting chat threads to registered client routes by resolving
/// candidate identities to emails. Fail-open by design (PLAN.md):
/// any resolution failure logs a structured event and leaves the
/// meeting on the normal bootstrap-fallback path — it never blocks
/// publishing.
/// </summary>
public sealed class ClientRouteResolver
{
    private readonly ClientRouteStore _store;
    private readonly GraphMetadataResolver _metadata;
    private readonly string? _tenantId;
    private readonly ILogger<ClientRouteResolver> _logger;

    public ClientRouteResolver(
        ClientRouteStore store,
        GraphMetadataResolver metadata,
        Models.BotConfiguration botConfig,
        ILogger<ClientRouteResolver> logger)
    {
        _store = store ?? throw new ArgumentNullException(nameof(store));
        _metadata = metadata ?? throw new ArgumentNullException(nameof(metadata));
        _tenantId = botConfig?.TenantId;
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
    }

    /// <summary>
    /// True when a bind attempt is worth the work: at least one enabled
    /// client route exists and this thread isn't bound yet. Callers use
    /// this to skip TeamsInfo lookups on the hot message path.
    /// </summary>
    public bool NeedsBinding(string meetingChatThreadId) =>
        _store.HasEnabledRoutes && _store.GetMeetingRoute(meetingChatThreadId) is null;

    /// <summary>
    /// Tries each candidate in order; the first one whose email matches
    /// an enabled client route wins and the binding is persisted. Sticky:
    /// an existing binding is never overwritten.
    /// </summary>
    public async Task BindMeetingAsync(
        string meetingChatThreadId,
        string? meetingId,
        IReadOnlyList<ClientIdentityCandidate> candidates,
        CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(meetingChatThreadId) || candidates.Count == 0) return;
        if (!NeedsBinding(meetingChatThreadId)) return;

        foreach (var candidate in candidates)
        {
            string? email;
            try
            {
                email = await ResolveEmailAsync(candidate, cancellationToken);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex,
                    "client_route_unresolved meeting_chat_thread_id={Thread} aad_object_id={Aad} candidate_source={Source} reason=resolver_error",
                    meetingChatThreadId, candidate.AadObjectId ?? "(null)", candidate.Source);
                continue;
            }

            if (string.IsNullOrWhiteSpace(email))
            {
                _logger.LogInformation(
                    "client_route_unresolved meeting_chat_thread_id={Thread} aad_object_id={Aad} candidate_source={Source} reason=missing_mail",
                    meetingChatThreadId, candidate.AadObjectId ?? "(null)", candidate.Source);
                continue;
            }

            var route = _store.GetRoute(email);
            if (route is null || !route.Enabled)
            {
                _logger.LogInformation(
                    "client_route_missing email={Email} candidate_source={Source} meeting_chat_thread_id={Thread}",
                    email, candidate.Source, meetingChatThreadId);
                continue;
            }

            await _store.UpsertMeetingRouteAsync(
                new MeetingRouteRecord
                {
                    MeetingChatThreadId = meetingChatThreadId,
                    MeetingId = meetingId,
                    Email = route.Email,
                    Source = candidate.Source,
                },
                cancellationToken);

            _logger.LogInformation(
                "client_route_bound meeting_chat_thread_id={Thread} email={Email} source={Source} sink_url={SinkUrl}",
                meetingChatThreadId, route.Email, candidate.Source, route.SinkUrl);
            return;
        }
    }

    /// <summary>
    /// Email resolution chain: caller-supplied (TeamsInfo) → persisted
    /// alias → Graph <c>GET /users/{id}</c> (mail, then UPN). Successful
    /// resolutions are written back to the alias table.
    /// </summary>
    private async Task<string?> ResolveEmailAsync(
        ClientIdentityCandidate candidate, CancellationToken cancellationToken)
    {
        if (!string.IsNullOrWhiteSpace(candidate.Email))
        {
            if (!string.IsNullOrWhiteSpace(candidate.AadObjectId))
            {
                await _store.UpsertAliasAsync(
                    new ClientIdentityAliasRecord
                    {
                        Email = candidate.Email!,
                        TenantId = _tenantId,
                        AadObjectId = candidate.AadObjectId!,
                        Source = "teams_activity",
                    },
                    cancellationToken);
            }
            return ClientRouteRecord.NormalizeEmail(candidate.Email!);
        }

        if (string.IsNullOrWhiteSpace(candidate.AadObjectId)) return null;

        var alias = _store.GetAliasEmail(candidate.AadObjectId!);
        if (!string.IsNullOrWhiteSpace(alias)) return alias;

        var user = await _metadata.GetUserAsync(candidate.AadObjectId!, cancellationToken);
        var email = user?.Mail ?? user?.UserPrincipalName;
        if (string.IsNullOrWhiteSpace(email)) return null;

        await _store.UpsertAliasAsync(
            new ClientIdentityAliasRecord
            {
                Email = email!,
                TenantId = _tenantId,
                AadObjectId = candidate.AadObjectId!,
                Source = "graph_user_lookup",
            },
            cancellationToken);
        return ClientRouteRecord.NormalizeEmail(email!);
    }
}
