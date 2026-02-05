using Microsoft.Graph.Communications.Calls;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Graph.Communications.Client;
using Microsoft.Graph.Communications.Client.Authentication;
using Microsoft.Graph.Communications.Common;
using Microsoft.Graph.Communications.Common.Telemetry;
using Microsoft.Graph.Communications.Resources;
using Microsoft.Identity.Client;
using Microsoft.IdentityModel.Protocols;
using Microsoft.IdentityModel.Protocols.OpenIdConnect;
using Microsoft.IdentityModel.Tokens;
using System.Collections.Concurrent;
using System.IdentityModel.Tokens.Jwt;
using System.Net;
using System.Net.Http.Headers;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Security.Claims;
using System.Text;
using System.Text.RegularExpressions;
using TeamsMediaBot.Models;
using Microsoft.Graph.Models;
using Microsoft.Graph.Contracts;
using Microsoft.Skype.Bots.Media;

namespace TeamsMediaBot.Services;

/// <summary>
/// Core bot service for joining Teams meetings and receiving real-time audio.
/// </summary>
/// <remarks>
/// <para>
/// Based on Microsoft's EchoBot sample and Graph Communications SDK patterns.
/// </para>
/// <para>
/// Key features:
/// <list type="bullet">
///   <item>ConcurrentDictionary for thread-safe call management</item>
///   <item>Global call event subscriptions (OnIncoming, OnUpdated)</item>
///   <item>CallHandler pattern with heartbeat keepalive</item>
///   <item>VideoSocketSettings even when video inactive (required by SDK)</item>
/// </list>
/// </para>
/// </remarks>
public sealed partial class TeamsCallingBotService : IAsyncDisposable
{
    private readonly BotConfiguration _botConfig;
    private readonly MediaPlatformConfiguration _mediaConfig;
    private readonly ILogger<TeamsCallingBotService> _logger;
    private readonly IGraphLogger _graphLogger;
    private readonly TranscriberFactory _transcriberFactory;
    private readonly ConcurrentDictionary<string, IRealtimeTranscriber> _pendingTranscribers = new();

    private ICommunicationsClient? _client;
    private bool _isDisposed;
    
    /// <summary>
    /// Thread-safe dictionary of active call handlers, keyed by thread ID.
    /// </summary>
    /// <remarks>
    /// Per Microsoft EchoBot sample: Uses ConcurrentDictionary for thread safety
    /// when handling concurrent call events from multiple threads.
    /// </remarks>
    public ConcurrentDictionary<string, CallHandler> CallHandlers { get; } = new();

    /// <summary>
    /// Gets the Graph Communications client for notification processing.
    /// </summary>
    /// <exception cref="InvalidOperationException">
    /// Thrown when the bot service has not been initialized.
    /// </exception>
    public ICommunicationsClient Client => _client 
        ?? throw new InvalidOperationException("Bot service not initialized. Call InitializeAsync first.");

    /// <summary>
    /// Source-generated regex for parsing short Teams meeting URLs.
    /// Format: https://teams.microsoft.com/meet/{meetingId}?p={passcode}
    /// </summary>
    [GeneratedRegex(@"teams\.microsoft\.com/meet/(?<meetingId>[^?]+)(\?p=(?<passcode>[^&]+))?", RegexOptions.Compiled | RegexOptions.CultureInvariant)]
    private static partial Regex ShortMeetingUrlRegex();

    /// <summary>
    /// Source-generated regex for parsing legacy Teams meeting URLs.
    /// Format: https://teams.microsoft.com/l/meetup-join/{thread}/{message}?context={...}
    /// </summary>
    [GeneratedRegex(@"https://teams\.microsoft\.com.*/(?<thread>[^/]+)/(?<message>[^/]+)\?context=(?<context>\{.*\})", RegexOptions.Compiled | RegexOptions.CultureInvariant)]
    private static partial Regex LegacyMeetingUrlRegex();

    /// <summary>
    /// Initializes a new instance of the <see cref="TeamsCallingBotService"/> class.
    /// </summary>
    /// <param name="botConfig">The bot configuration.</param>
    /// <param name="mediaConfig">The media platform configuration.</param>
    /// <param name="logger">The logger instance.</param>
    /// <param name="graphLogger">The Graph Communications logger.</param>
    /// <param name="serviceProvider">The service provider (unused, kept for DI compatibility).</param>
    /// <param name="transcriberFactory">The factory for creating transcribers.</param>
    /// <exception cref="ArgumentNullException">Thrown when required parameters are null.</exception>
    public TeamsCallingBotService(
        BotConfiguration botConfig,
        MediaPlatformConfiguration mediaConfig,
        ILogger<TeamsCallingBotService> logger,
        IGraphLogger graphLogger,
        IServiceProvider serviceProvider,
        TranscriberFactory transcriberFactory)
    {
        ArgumentNullException.ThrowIfNull(botConfig);
        ArgumentNullException.ThrowIfNull(mediaConfig);
        ArgumentNullException.ThrowIfNull(logger);
        ArgumentNullException.ThrowIfNull(graphLogger);
        ArgumentNullException.ThrowIfNull(transcriberFactory);
        
        _botConfig = botConfig;
        _mediaConfig = mediaConfig;
        _logger = logger;
        _graphLogger = graphLogger;
        _transcriberFactory = transcriberFactory;
        // Note: serviceProvider is kept in signature for DI but not used
    }

    /// <summary>
    /// Initializes the Graph Communications client and subscribes to call events.
    /// </summary>
    /// <remarks>
    /// Must configure MediaPlatformSettings for app-hosted media per Microsoft documentation.
    /// </remarks>
    /// <returns>A task representing the asynchronous initialization.</returns>
    public Task InitializeAsync()
    {
        ObjectDisposedException.ThrowIf(_isDisposed, this);
        
        _logger.LogInformation("Initializing Teams Calling Bot Service...");

        // Create authentication provider for token acquisition and validation
        var authProvider = new AuthenticationProvider(
            _botConfig.AppId,
            _botConfig.AppSecret,
            _botConfig.TenantId,
            _logger);

        // Configure media platform settings for app-hosted media
        // NOTE: MediaPlatformSettings is from Microsoft.Skype.Bots.Media (not Graph.Communications)
        var mediaPlatformSettings = new MediaPlatformSettings
        {
            ApplicationId = _mediaConfig.ApplicationId,
            MediaPlatformInstanceSettings = new MediaPlatformInstanceSettings
            {
                CertificateThumbprint = _mediaConfig.CertificateThumbprint,
                InstanceInternalPort = _mediaConfig.InstanceInternalPort,
                InstancePublicPort = _mediaConfig.InstancePublicPort,
                InstancePublicIPAddress = IPAddress.Parse(_mediaConfig.InstancePublicIPAddress),
                ServiceFqdn = _mediaConfig.ServiceFqdn
            }
        };

        _logger.LogInformation(
            "Media platform settings: FQDN={ServiceFqdn}, InternalPort={InternalPort}, PublicPort={PublicPort}, CertThumbprint={CertThumbprint}",
            mediaPlatformSettings.MediaPlatformInstanceSettings.ServiceFqdn,
            mediaPlatformSettings.MediaPlatformInstanceSettings.InstanceInternalPort,
            mediaPlatformSettings.MediaPlatformInstanceSettings.InstancePublicPort,
            mediaPlatformSettings.MediaPlatformInstanceSettings.CertificateThumbprint);

        // Build communications client
        var builder = new CommunicationsClientBuilder(
            "TeamsMediaBotPOC",
            _botConfig.AppId,
            _graphLogger);

        builder
            .SetAuthenticationProvider(authProvider)
            .SetNotificationUrl(new Uri(_botConfig.NotificationUrl))
            .SetMediaPlatformSettings(mediaPlatformSettings)
            .SetServiceBaseUrl(new Uri("https://graph.microsoft.com/v1.0"));

        _client = builder.Build();

        // Subscribe to global call events (required for proper SDK state management)
        _client.Calls().OnIncoming += OnCallsIncoming;
        _client.Calls().OnUpdated += OnCallsUpdated;

        _logger.LogInformation("Teams Calling Bot Service initialized successfully");
        
        return Task.CompletedTask;
    }

    /// <summary>
    /// Handles incoming calls (e.g., when bot is added to an existing call).
    /// </summary>
    /// <remarks>
    /// Per Microsoft samples: Answer incoming calls with media session.
    /// </remarks>
    private void OnCallsIncoming(ICallCollection sender, CollectionEventArgs<ICall> args)
    {
        foreach (var call in args.AddedResources)
        {
            _logger.LogInformation(
                "Incoming call received: CallId={CallId}, ObservedParticipantId={ObservedParticipantId}",
                call.Id,
                call.Resource.IncomingContext?.ObservedParticipantId);

            // For incoming calls, we need to answer them
            var mediaSession = CreateMediaSession(call.Id);
            
            // Fire-and-forget answer - SDK handles state management
            _ = AnswerCallAsync(call, mediaSession);
        }
    }

    /// <summary>
    /// Answers an incoming call asynchronously.
    /// </summary>
    private async Task AnswerCallAsync(ICall call, ILocalMediaSession mediaSession)
    {
        try
        {
            await call.AnswerAsync(mediaSession).ConfigureAwait(false);
            _logger.LogInformation("Answered incoming call: {CallId}", call.Id);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to answer incoming call: {CallId}", call.Id);
        }
    }

    /// <summary>
    /// Handles call collection updates (added/removed calls).
    /// </summary>
    /// <remarks>
    /// Per Microsoft samples: Create CallHandler for new calls, cleanup for removed.
    /// </remarks>
    private void OnCallsUpdated(ICallCollection sender, CollectionEventArgs<ICall> args)
    {
        // Handle added calls
        foreach (var call in args.AddedResources)
        {
            HandleCallAdded(call);
        }

        // Handle removed calls
        foreach (var call in args.RemovedResources)
        {
            HandleCallRemoved(call);
        }
    }

    /// <summary>
    /// Handles a call being added to the collection.
    /// </summary>
    private void HandleCallAdded(ICall call)
    {
        var threadId = call.Resource.ChatInfo?.ThreadId ?? call.Id;
        if (string.IsNullOrWhiteSpace(threadId))
        {
            _logger.LogWarning("Call added with no threadId or callId; skipping handler creation");
            return;
        }
        
        // Check if we already have a handler for this call
        if (CallHandlers.ContainsKey(threadId))
        {
            return;
        }

        _logger.LogInformation(
            "Call added to collection: CallId={CallId}, ThreadId={ThreadId}",
            call.Id,
            threadId);
        
        // Get or create transcriber (may have been pre-registered by JoinMeetingAsync)
        var transcriber = GetOrCreateTranscriber(threadId);
        
        // Get media session from call
        var mediaSession = call.GetLocalMediaSession();
        
        // Create handler with heartbeat keepalive
        var handler = new CallHandler(call, mediaSession, transcriber, _logger);
        CallHandlers[threadId] = handler;
        
        _logger.LogInformation(
            "Created CallHandler for thread: {ThreadId} (heartbeat enabled every 10 min)", 
            threadId);
    }

    /// <summary>
    /// Handles a call being removed from the collection.
    /// </summary>
    private void HandleCallRemoved(ICall call)
    {
        var threadId = call.Resource.ChatInfo?.ThreadId ?? call.Id;
        if (string.IsNullOrWhiteSpace(threadId))
        {
            _logger.LogWarning("Call removed with no threadId or callId; skipping handler cleanup");
            return;
        }
        
        if (CallHandlers.TryRemove(threadId, out var handler))
        {
            _logger.LogInformation("Removing CallHandler for thread: {ThreadId}", threadId);
            
            // Fire-and-forget cleanup
            _ = CleanupHandlerAsync(handler, threadId);
        }
        
        // Clean up any pending transcribers that weren't used
        _pendingTranscribers.TryRemove(threadId, out _);
    }

    /// <summary>
    /// Cleans up a call handler asynchronously.
    /// </summary>
    private async Task CleanupHandlerAsync(CallHandler handler, string threadId)
    {
        try
        {
            await handler.ShutdownAsync().ConfigureAwait(false);
            handler.Dispose();
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error disposing CallHandler for thread: {ThreadId}", threadId);
        }
    }

    /// <summary>
    /// Creates a local media session with audio and video socket settings.
    /// </summary>
    /// <remarks>
    /// Per Microsoft samples: Include VideoSocketSettings even when video is inactive.
    /// </remarks>
    /// <param name="callId">Optional call ID to use as the media session ID.</param>
    /// <returns>The configured local media session.</returns>
    private ILocalMediaSession CreateMediaSession(string? callId = null)
    {
        var mediaSessionId = Guid.Empty;
        if (!string.IsNullOrEmpty(callId) && Guid.TryParse(callId, out var parsedId))
        {
            mediaSessionId = parsedId;
        }

        return Client.CreateMediaSession(
            new AudioSocketSettings
            {
                StreamDirections = StreamDirection.Recvonly,  // We only need to receive audio
                SupportedAudioFormat = AudioFormat.Pcm16K,    // 16 kHz PCM
                ReceiveUnmixedMeetingAudio = false            // Mixed audio is fine for transcription
            },
            new VideoSocketSettings
            {
                StreamDirections = StreamDirection.Inactive   // No video needed
            },
            mediaSessionId: mediaSessionId);
    }

    /// <summary>
    /// Joins a Teams meeting using the provided join URL.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Supports two URL formats:
    /// <list type="number">
    ///   <item>New format: https://teams.microsoft.com/meet/{meetingId}?p={passcode}</item>
    ///   <item>Legacy format: https://teams.microsoft.com/l/meetup-join/{thread}/{message}?context={...}</item>
    /// </list>
    /// </para>
    /// <para>
    /// CallHandler creation is handled by OnCallsUpdated event.
    /// This method just initiates the join; SDK events handle the rest.
    /// </para>
    /// </remarks>
    /// <param name="joinUrl">The Teams meeting join URL.</param>
    /// <param name="displayName">The display name for the bot in the meeting.</param>
    /// <param name="joinAsGuest">Whether to join as a guest with display name.</param>
    /// <param name="transcriber">The transcriber to use for this call.</param>
    /// <returns>The call ID assigned by the Graph Communications SDK.</returns>
    /// <exception cref="InvalidOperationException">
    /// Thrown when the bot service is not initialized or the bot is already in the meeting.
    /// </exception>
    /// <exception cref="ArgumentException">Thrown when the join URL cannot be parsed.</exception>
    public async Task<string> JoinMeetingAsync(
        string joinUrl,
        string displayName,
        bool joinAsGuest,
        IRealtimeTranscriber transcriber)
    {
        ObjectDisposedException.ThrowIf(_isDisposed, this);
        ArgumentException.ThrowIfNullOrWhiteSpace(joinUrl);
        ArgumentNullException.ThrowIfNull(transcriber);
        
        if (_client is null)
        {
            throw new InvalidOperationException("Bot service not initialized. Call InitializeAsync first.");
        }

        _logger.LogInformation(
            "Joining meeting: JoinUrl={JoinUrl}, DisplayName={DisplayName}, JoinAsGuest={JoinAsGuest}",
            joinUrl, displayName, joinAsGuest);

        // A tracking id for logging purposes
        var scenarioId = Guid.NewGuid();

        // Create media session with both audio and video settings
        var mediaSession = CreateMediaSession();

        ICall call;
        string threadId;

        // Check if this is the new short URL format
        var shortUrlMatch = ShortMeetingUrlRegex().Match(joinUrl);
        
        if (shortUrlMatch.Success)
        {
            (call, threadId) = await JoinMeetingWithShortUrlAsync(
                shortUrlMatch, mediaSession, scenarioId, displayName, joinAsGuest, transcriber)
                .ConfigureAwait(false);
        }
        else
        {
            (call, threadId) = await JoinMeetingWithLegacyUrlAsync(
                joinUrl, mediaSession, scenarioId, displayName, joinAsGuest, transcriber)
                .ConfigureAwait(false);
        }

        _logger.LogInformation("Call created: CallId={CallId}, ThreadId={ThreadId}", call.Id, threadId);

        return call.Id;
    }

    /// <summary>
    /// Joins a meeting using the short URL format.
    /// </summary>
    private async Task<(ICall Call, string ThreadId)> JoinMeetingWithShortUrlAsync(
        Match shortUrlMatch,
        ILocalMediaSession mediaSession,
        Guid scenarioId,
        string displayName,
        bool joinAsGuest,
        IRealtimeTranscriber transcriber)
    {
        var meetingId = shortUrlMatch.Groups["meetingId"].Value;
        var passcode = shortUrlMatch.Groups["passcode"].Success ? shortUrlMatch.Groups["passcode"].Value : null;
        
        _logger.LogInformation(
            "Using JoinMeetingId format: MeetingId={MeetingId}, HasPasscode={HasPasscode}", 
            meetingId,
            !string.IsNullOrEmpty(passcode));

        var meetingInfo = new JoinMeetingIdMeetingInfo
        {
            JoinMeetingId = meetingId,
            Passcode = passcode
        };

        // For short URLs, we don't have chatInfo, so use a generated thread ID
        var threadId = $"meet-{meetingId}";

        // Check if we already have a handler
        if (CallHandlers.ContainsKey(threadId))
        {
            _logger.LogWarning("Call handler already exists for meeting: {MeetingId}", meetingId);
            throw new InvalidOperationException($"Bot is already in this meeting: {meetingId}");
        }

        // Store transcriber for when CallHandler is created
        _pendingTranscribers[threadId] = transcriber;

        var joinParams = new JoinMeetingParameters(null, meetingInfo, mediaSession)
        {
            TenantId = _botConfig.TenantId
        };

        ConfigureGuestIdentity(joinParams, displayName, joinAsGuest);

        var call = await _client!.Calls().AddAsync(joinParams, scenarioId).ConfigureAwait(false);
        return (call, threadId);
    }

    /// <summary>
    /// Joins a meeting using the legacy URL format.
    /// </summary>
    private async Task<(ICall Call, string ThreadId)> JoinMeetingWithLegacyUrlAsync(
        string joinUrl,
        ILocalMediaSession mediaSession,
        Guid scenarioId,
        string displayName,
        bool joinAsGuest,
        IRealtimeTranscriber transcriber)
    {
        var (chatInfo, meetingInfo) = ParseJoinUrl(joinUrl);

        var tenantId = meetingInfo.Organizer?.GetPrimaryIdentity()?.GetTenantId();
        var joinParams = new JoinMeetingParameters(chatInfo, meetingInfo, mediaSession)
        {
            TenantId = string.IsNullOrWhiteSpace(tenantId) ? _botConfig.TenantId : tenantId
        };

        ConfigureGuestIdentity(joinParams, displayName, joinAsGuest);

        // Check if we already have a handler for this thread
        if (string.IsNullOrWhiteSpace(chatInfo.ThreadId))
        {
            throw new ArgumentException("Join URL did not contain a valid threadId", nameof(joinUrl));
        }

        var threadId = chatInfo.ThreadId;
        if (CallHandlers.ContainsKey(threadId))
        {
            _logger.LogWarning("Call handler already exists for thread: {ThreadId}", threadId);
            throw new InvalidOperationException($"Bot is already in a call for thread: {threadId}");
        }

        // Store transcriber for when CallHandler is created
        _pendingTranscribers[threadId] = transcriber;

        var call = await _client!.Calls().AddAsync(joinParams, scenarioId).ConfigureAwait(false);
        return (call, threadId);
    }

    /// <summary>
    /// Configures the guest identity on join parameters if joining as guest.
    /// </summary>
    private void ConfigureGuestIdentity(JoinMeetingParameters joinParams, string displayName, bool joinAsGuest)
    {
        if (joinAsGuest && !string.IsNullOrWhiteSpace(displayName))
        {
            joinParams.GuestIdentity = new Identity
            {
                Id = Guid.NewGuid().ToString(),
                DisplayName = displayName
            };
        }
        else if (!joinAsGuest && !string.IsNullOrWhiteSpace(displayName))
        {
            _logger.LogInformation(
                "DisplayName provided but joinAsGuest=false; joining as app identity. DisplayName ignored.");
        }
    }

    /// <summary>
    /// Gets or creates a transcriber for the given thread.
    /// </summary>
    /// <param name="threadId">The thread ID to get a transcriber for.</param>
    /// <returns>The transcriber for the thread.</returns>
    internal IRealtimeTranscriber GetOrCreateTranscriber(string threadId)
    {
        if (_pendingTranscribers.TryRemove(threadId, out var transcriber))
        {
            return transcriber;
        }

        // If no pending transcriber, create a new one using the factory
        return _transcriberFactory.Create();
    }

    /// <summary>
    /// Parses a legacy Teams join URL into ChatInfo and MeetingInfo.
    /// </summary>
    /// <param name="joinUrl">The Teams meeting join URL.</param>
    /// <returns>A tuple of ChatInfo and OrganizerMeetingInfo.</returns>
    /// <exception cref="ArgumentException">Thrown when the URL cannot be parsed.</exception>
    private (ChatInfo chatInfo, OrganizerMeetingInfo meetingInfo) ParseJoinUrl(string joinUrl)
    {
        var decodedUrl = WebUtility.UrlDecode(joinUrl);
        var match = LegacyMeetingUrlRegex().Match(decodedUrl);

        if (!match.Success)
        {
            throw new ArgumentException($"Join URL cannot be parsed: {joinUrl}.", nameof(joinUrl));
        }

        JoinUrlContext context;
        using (var stream = new MemoryStream(Encoding.UTF8.GetBytes(match.Groups["context"].Value)))
        {
            var serializer = new DataContractJsonSerializer(typeof(JoinUrlContext));
            context = (JoinUrlContext?)serializer.ReadObject(stream) 
                ?? throw new ArgumentException("Join URL context is null", nameof(joinUrl));
        }

        var chatInfo = new ChatInfo
        {
            ThreadId = match.Groups["thread"].Value,
            MessageId = match.Groups["message"].Value,
            ReplyChainMessageId = context.MessageId
        };

        var meetingInfo = new OrganizerMeetingInfo
        {
            Organizer = new IdentitySet
            {
                User = new Identity { Id = context.Oid }
            }
        };
        meetingInfo.Organizer.User.SetTenantId(context.Tid);

        return (chatInfo, meetingInfo);
    }

    /// <summary>
    /// Gracefully shuts down the bot service, terminating all active calls.
    /// </summary>
    /// <returns>A task representing the asynchronous shutdown operation.</returns>
    public async Task ShutdownAsync()
    {
        if (_isDisposed)
        {
            return;
        }

        _logger.LogWarning("Shutting down bot service - terminating all active calls");

        if (_client is not null)
        {
            // Unsubscribe from events first
            _client.Calls().OnIncoming -= OnCallsIncoming;
            _client.Calls().OnUpdated -= OnCallsUpdated;

            // Terminate all active calls
            try
            {
                await _client.TerminateAsync().ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error terminating Graph Communications client");
            }
        }

        // Dispose all handlers concurrently
        var cleanupTasks = CallHandlers.Select(async kvp =>
        {
            try
            {
                await kvp.Value.ShutdownAsync().ConfigureAwait(false);
                kvp.Value.Dispose();
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error disposing handler for thread: {ThreadId}", kvp.Key);
            }
        });

        await Task.WhenAll(cleanupTasks).ConfigureAwait(false);
        
        CallHandlers.Clear();
        _pendingTranscribers.Clear();

        _logger.LogInformation("Bot service shutdown complete");
    }

    /// <inheritdoc/>
    public async ValueTask DisposeAsync()
    {
        if (_isDisposed)
        {
            return;
        }

        _isDisposed = true;
        await ShutdownAsync().ConfigureAwait(false);
        
        _client?.Dispose();
        GC.SuppressFinalize(this);
    }
}

/// <summary>
/// Authentication provider for Graph Communications SDK.
/// </summary>
/// <remarks>
/// <para>
/// Implements production-grade JWT validation and token caching based on Microsoft's official EchoBot sample.
/// </para>
/// <para>
/// Sources:
/// <list type="bullet">
///   <item>https://github.com/microsoftgraph/microsoft-graph-comms-samples</item>
///   <item>https://microsoftgraph.github.io/microsoft-graph-comms-samples/docs/articles/calls/calling-notifications.html</item>
/// </list>
/// </para>
/// </remarks>
internal sealed class AuthenticationProvider : IRequestAuthenticationProvider
{
    /// <summary>
    /// OpenID Connect configuration endpoint for Skype/Teams authentication.
    /// </summary>
    private const string AuthDomain = "https://api.aps.skype.com/v1/.well-known/OpenIdConfiguration";
    
    /// <summary>
    /// Microsoft Graph resource identifier for token requests.
    /// </summary>
    private const string GraphResource = "https://graph.microsoft.com";
    
    /// <summary>
    /// Interval for refreshing OpenID configuration (2 hours).
    /// </summary>
    private static readonly TimeSpan OpenIdConfigRefreshInterval = TimeSpan.FromHours(2);
    
    /// <summary>
    /// Valid issuers for JWT validation.
    /// </summary>
    private static readonly string[] ValidIssuers =
    [
        "https://graph.microsoft.com",
        "https://api.botframework.com"
    ];
    
    private readonly string _clientId;
    private readonly string _clientSecret;
    private readonly string _defaultTenantId;
    private readonly ILogger _logger;
    private readonly IConfidentialClientApplication _msalApp;
    private readonly SemaphoreSlim _configLock = new(1, 1);
    
    private DateTime _prevOpenIdConfigUpdateTimestamp = DateTime.MinValue;
    private OpenIdConnectConfiguration? _openIdConfiguration;

    /// <summary>
    /// Initializes a new instance of the <see cref="AuthenticationProvider"/> class.
    /// </summary>
    /// <param name="clientId">The Azure AD application (client) ID.</param>
    /// <param name="clientSecret">The application client secret.</param>
    /// <param name="tenantId">The default tenant ID.</param>
    /// <param name="logger">The logger instance.</param>
    /// <exception cref="ArgumentNullException">Thrown when required parameters are null.</exception>
    public AuthenticationProvider(string clientId, string clientSecret, string tenantId, ILogger logger)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(clientId);
        ArgumentException.ThrowIfNullOrWhiteSpace(clientSecret);
        ArgumentException.ThrowIfNullOrWhiteSpace(tenantId);
        ArgumentNullException.ThrowIfNull(logger);
        
        _clientId = clientId;
        _clientSecret = clientSecret;
        _defaultTenantId = tenantId;
        _logger = logger;
        
        // Create singleton MSAL application for token caching
        // Per MSAL best practices: https://learn.microsoft.com/en-us/entra/msal/dotnet/getting-started/best-practices
        var authority = $"https://login.microsoftonline.com/{_defaultTenantId}";
        _msalApp = ConfidentialClientApplicationBuilder
            .Create(_clientId)
            .WithClientSecret(_clientSecret)
            .WithAuthority(authority)
            .Build();
            
        _logger.LogInformation("AuthenticationProvider initialized with singleton token cache");
    }

    /// <summary>
    /// Authenticates outbound requests to Microsoft Graph.
    /// </summary>
    /// <remarks>
    /// Uses cached tokens when available (MSAL handles caching automatically).
    /// Per Microsoft recommendation: Token caching improves performance and prevents throttling.
    /// </remarks>
    public async Task AuthenticateOutboundRequestAsync(HttpRequestMessage request, string tenant)
    {
        ArgumentNullException.ThrowIfNull(request);
        
        var scopes = new[] { $"{GraphResource}/.default" };
        var resolvedTenant = string.IsNullOrWhiteSpace(tenant) ? _defaultTenantId : tenant;
        if (string.IsNullOrWhiteSpace(resolvedTenant))
        {
            resolvedTenant = "common";
        }

        _logger.LogDebug("Acquiring token for tenant: {TenantId}", resolvedTenant);

        try
        {
            // If tenant differs from default, create temporary app for that tenant
            // Otherwise use singleton (which has cached tokens)
            IConfidentialClientApplication app;
            if (!string.Equals(resolvedTenant, _defaultTenantId, StringComparison.OrdinalIgnoreCase))
            {
                var authority = $"https://login.microsoftonline.com/{resolvedTenant}";
                app = ConfidentialClientApplicationBuilder
                    .Create(_clientId)
                    .WithClientSecret(_clientSecret)
                    .WithAuthority(authority)
                    .Build();
                _logger.LogDebug("Created temporary MSAL app for tenant: {TenantId}", resolvedTenant);
            }
            else
            {
                app = _msalApp;
            }

            // Acquire token with retry (MSAL automatically uses cache)
            var result = await AcquireTokenWithRetryAsync(app, scopes, maxAttempts: 3)
                .ConfigureAwait(false);
            
            _logger.LogDebug(
                "Token acquired successfully. ExpiresInMinutes={ExpiresInMinutes:F1}", 
                result.ExpiresOn.Subtract(DateTimeOffset.UtcNow).TotalMinutes);

            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", result.AccessToken);
        }
        catch (Exception ex)
        {
            _logger.LogError(
                ex, 
                "Failed to acquire token for client: {ClientId}, tenant: {TenantId}",
                _clientId, 
                resolvedTenant);
            throw;
        }
    }

    /// <summary>
    /// Validates inbound requests from Microsoft Graph.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Implements production-grade JWT validation per Microsoft security requirements.
    /// </para>
    /// <para>
    /// Security checks:
    /// <list type="number">
    ///   <item>Validates JWT signature using OpenID Connect configuration</item>
    ///   <item>Verifies issuers: https://graph.microsoft.com and https://api.botframework.com</item>
    ///   <item>Validates audience matches App ID</item>
    ///   <item>Extracts tenant ID from token claims</item>
    /// </list>
    /// </para>
    /// <para>
    /// Per Microsoft: Returning IsValid=false triggers 403 Forbidden response.
    /// </para>
    /// </remarks>
    public async Task<RequestValidationResult> ValidateInboundRequestAsync(HttpRequestMessage request)
    {
        if (request is null)
        {
            _logger.LogWarning("Inbound request validation failed: Request is null");
            return new RequestValidationResult { IsValid = false };
        }

        var token = request.Headers?.Authorization?.Parameter;
        if (string.IsNullOrWhiteSpace(token))
        {
            _logger.LogWarning("Inbound request validation failed: No Authorization token provided");
            return new RequestValidationResult { IsValid = false };
        }

        // Update OpenID configuration if needed (cached for 2 hours)
        await EnsureOpenIdConfigurationAsync().ConfigureAwait(false);

        // Configure token validation parameters
        var validationParameters = new TokenValidationParameters
        {
            ValidIssuers = ValidIssuers,
            ValidAudience = _clientId,
            IssuerSigningKeys = _openIdConfiguration!.SigningKeys,
        };

        ClaimsPrincipal claimsPrincipal;
        try
        {
            // Validate token signature, expiration, issuer, audience
            var handler = new JwtSecurityTokenHandler();
            claimsPrincipal = handler.ValidateToken(token, validationParameters, out _);
            
            _logger.LogDebug("JWT token validation successful");
        }
        catch (SecurityTokenExpiredException ex)
        {
            _logger.LogWarning(ex, "Inbound request validation failed: Token expired");
            return new RequestValidationResult { IsValid = false };
        }
        catch (SecurityTokenInvalidSignatureException ex)
        {
            _logger.LogWarning(ex, "Inbound request validation failed: Invalid token signature (possible tampering)");
            return new RequestValidationResult { IsValid = false };
        }
        catch (SecurityTokenValidationException ex)
        {
            _logger.LogWarning(ex, "Inbound request validation failed: Token validation error");
            return new RequestValidationResult { IsValid = false };
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Inbound request validation failed: Unexpected error for client: {ClientId}", _clientId);
            return new RequestValidationResult { IsValid = false };
        }

        // Extract tenant ID from token claims
        const string TenantIdClaimType = "http://schemas.microsoft.com/identity/claims/tenantid";
        var tenantClaim = claimsPrincipal.FindFirst(claim => 
            string.Equals(claim.Type, TenantIdClaimType, StringComparison.Ordinal));

        if (string.IsNullOrEmpty(tenantClaim?.Value))
        {
            _logger.LogWarning("Inbound request validation failed: No tenant claim in token");
            return new RequestValidationResult { IsValid = false };
        }

        _logger.LogDebug("Request validated successfully for tenant: {TenantId}", tenantClaim.Value);
        
        // Store tenant in request options for SDK use
        request.Options.Set(new HttpRequestOptionsKey<string>("Microsoft-Tenant-Id"), tenantClaim.Value);
        
        return new RequestValidationResult 
        { 
            IsValid = true, 
            TenantId = tenantClaim.Value 
        };
    }

    /// <summary>
    /// Ensures the OpenID configuration is loaded and refreshed if needed.
    /// </summary>
    private async Task EnsureOpenIdConfigurationAsync()
    {
        if (_openIdConfiguration is not null && 
            DateTime.UtcNow <= _prevOpenIdConfigUpdateTimestamp.Add(OpenIdConfigRefreshInterval))
        {
            return;
        }

        await _configLock.WaitAsync().ConfigureAwait(false);
        try
        {
            // Double-check after acquiring lock
            if (_openIdConfiguration is not null && 
                DateTime.UtcNow <= _prevOpenIdConfigUpdateTimestamp.Add(OpenIdConfigRefreshInterval))
            {
                return;
            }

            _logger.LogInformation("Updating OpenID configuration from {AuthDomain}", AuthDomain);

            // Download the OIDC configuration which contains the JWKS
            var configurationManager = new ConfigurationManager<OpenIdConnectConfiguration>(
                AuthDomain,
                new OpenIdConnectConfigurationRetriever());
            
            _openIdConfiguration = await configurationManager
                .GetConfigurationAsync(CancellationToken.None)
                .ConfigureAwait(false);
            
            _prevOpenIdConfigUpdateTimestamp = DateTime.UtcNow;
            
            _logger.LogInformation(
                "OpenID configuration updated. SigningKeyCount={SigningKeyCount}", 
                _openIdConfiguration.SigningKeys.Count);
        }
        finally
        {
            _configLock.Release();
        }
    }

    /// <summary>
    /// Acquires a token with retry logic for transient failures.
    /// </summary>
    /// <remarks>
    /// MSAL automatically uses cached tokens when available.
    /// </remarks>
    private async Task<AuthenticationResult> AcquireTokenWithRetryAsync(
        IConfidentialClientApplication app, 
        string[] scopes, 
        int maxAttempts)
    {
        var attempts = maxAttempts;
        
        while (true)
        {
            attempts--;

            try
            {
                return await app
                    .AcquireTokenForClient(scopes)
                    .ExecuteAsync()
                    .ConfigureAwait(false);
            }
            catch (MsalServiceException ex) when (ex.IsRetryable && attempts > 0)
            {
                _logger.LogWarning(
                    ex,
                    "Retryable token acquisition error. AttemptsRemaining={AttemptsRemaining}",
                    attempts);
            }
            catch (Exception ex) when (attempts > 0 && IsTransientError(ex))
            {
                _logger.LogWarning(
                    ex,
                    "Token acquisition failed with transient error. AttemptsRemaining={AttemptsRemaining}",
                    attempts);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Token acquisition failed after {AttemptCount} attempts", maxAttempts);
                throw;
            }

            // Exponential backoff: 1s, 2s, 4s...
            var delay = TimeSpan.FromSeconds(Math.Pow(2, maxAttempts - attempts - 1));
            await Task.Delay(delay).ConfigureAwait(false);
        }
    }

    /// <summary>
    /// Determines if an exception is a transient error that should be retried.
    /// </summary>
    private static bool IsTransientError(Exception ex)
    {
        return ex is HttpRequestException or TaskCanceledException;
    }
}

/// <summary>
/// Context data extracted from the Teams meeting join URL.
/// </summary>
[DataContract]
internal sealed class JoinUrlContext
{
    /// <summary>
    /// Gets or sets the tenant ID.
    /// </summary>
    [DataMember]
    public string Tid { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets the organizer object ID.
    /// </summary>
    [DataMember]
    public string Oid { get; set; } = string.Empty;

    /// <summary>
    /// Gets or sets the message ID.
    /// </summary>
    [DataMember]
    public string MessageId { get; set; } = string.Empty;
}
