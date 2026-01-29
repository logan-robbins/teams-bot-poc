using Microsoft.Graph;
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
/// Core bot service for joining Teams meetings and receiving real-time audio
/// Based on Part A of the validated guide and Microsoft's EchoBot sample
/// 
/// Key features (per Microsoft samples):
/// - ConcurrentDictionary for thread-safe call management
/// - Global call event subscriptions (OnIncoming, OnUpdated)
/// - CallHandler pattern with heartbeat keepalive
/// - VideoSocketSettings even when video inactive
/// 
/// Sources: S3, S4, S5, S6, S7, S10, S11, S14
/// </summary>
public class TeamsCallingBotService : IAsyncDisposable
{
    private readonly BotConfiguration _botConfig;
    private readonly MediaPlatformConfiguration _mediaConfig;
    private readonly ILogger<TeamsCallingBotService> _logger;
    private readonly IGraphLogger _graphLogger;
    private readonly IServiceProvider _serviceProvider;

    private ICommunicationsClient? _client;
    
    /// <summary>
    /// Thread-safe dictionary of active call handlers, keyed by thread ID
    /// Per Microsoft EchoBot sample: Uses ConcurrentDictionary for thread safety
    /// </summary>
    public ConcurrentDictionary<string, CallHandler> CallHandlers { get; } = new();

    /// <summary>
    /// Exposes the Graph Communications client for notification processing
    /// Required by CallingController to process webhook notifications
    /// </summary>
    public ICommunicationsClient Client => _client 
        ?? throw new InvalidOperationException("Bot service not initialized. Call InitializeAsync first.");

    public TeamsCallingBotService(
        BotConfiguration botConfig,
        MediaPlatformConfiguration mediaConfig,
        ILogger<TeamsCallingBotService> logger,
        IGraphLogger graphLogger,
        IServiceProvider serviceProvider)
    {
        _botConfig = botConfig;
        _mediaConfig = mediaConfig;
        _logger = logger;
        _graphLogger = graphLogger;
        _serviceProvider = serviceProvider;
    }

    /// <summary>
    /// Initialize the Graph Communications client
    /// Per S2, S11, S14: Must configure MediaPlatformSettings for app-hosted media
    /// </summary>
    public async Task InitializeAsync()
    {
        _logger.LogInformation("Initializing Teams Calling Bot Service...");

        // Create authentication provider per S13
        var authProvider = new AuthenticationProvider(
            _botConfig.AppId,
            _botConfig.AppSecret,
            _botConfig.TenantId,
            _logger);

        // Configure media platform settings per S2, S14
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
            "Media platform settings: FQDN={Fqdn}, InternalPort={InternalPort}, PublicPort={PublicPort}, Cert={Thumbprint}",
            mediaPlatformSettings.MediaPlatformInstanceSettings.ServiceFqdn,
            mediaPlatformSettings.MediaPlatformInstanceSettings.InstanceInternalPort,
            mediaPlatformSettings.MediaPlatformInstanceSettings.InstancePublicPort,
            mediaPlatformSettings.MediaPlatformInstanceSettings.CertificateThumbprint);

        // Build communications client per S10, S11
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

        // Subscribe to global call events (per Microsoft EchoBot sample)
        // This is required for proper SDK state management
        _client.Calls().OnIncoming += OnCallsIncoming;
        _client.Calls().OnUpdated += OnCallsUpdated;

        _logger.LogInformation("Teams Calling Bot Service initialized successfully");
    }

    /// <summary>
    /// Handles incoming calls (e.g., when bot is added to an existing call)
    /// Per Microsoft samples: Answer incoming calls with media session
    /// </summary>
    private void OnCallsIncoming(ICallCollection sender, CollectionEventArgs<ICall> args)
    {
        foreach (var call in args.AddedResources)
        {
            _logger.LogInformation(
                "Incoming call received: {CallId}, IncomingContext: {Context}",
                call.Id, call.Resource.IncomingContext?.ObservedParticipantId);

            // For incoming calls, we need to answer them
            // Create media session and answer
            var mediaSession = CreateMediaSession(call.Id);
            
            _ = Task.Run(async () =>
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
            });
        }
    }

    /// <summary>
    /// Handles call collection updates (added/removed calls)
    /// Per Microsoft samples: Create CallHandler for new calls, cleanup for removed
    /// </summary>
    private void OnCallsUpdated(ICallCollection sender, CollectionEventArgs<ICall> args)
    {
        // Handle added calls
        foreach (var call in args.AddedResources)
        {
            var threadId = call.Resource.ChatInfo?.ThreadId ?? call.Id;
            if (string.IsNullOrWhiteSpace(threadId))
            {
                _logger.LogWarning("Call added with no threadId or callId; skipping handler creation");
                continue;
            }
            
            // Check if we already have a handler for this call
            if (!CallHandlers.ContainsKey(threadId))
            {
                _logger.LogInformation(
                    "Call added to collection: {CallId}, ThreadId: {ThreadId}",
                    call.Id, threadId);
                
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
        }

        // Handle removed calls
        foreach (var call in args.RemovedResources)
        {
            var threadId = call.Resource.ChatInfo?.ThreadId ?? call.Id;
            if (string.IsNullOrWhiteSpace(threadId))
            {
                _logger.LogWarning("Call removed with no threadId or callId; skipping handler cleanup");
                continue;
            }
            
            if (CallHandlers.TryRemove(threadId, out var handler))
            {
                _logger.LogInformation("Removing CallHandler for thread: {ThreadId}", threadId);
                
                _ = Task.Run(async () =>
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
                });
            }
            
            // Clean up any pending transcribers that weren't used
            _pendingTranscribers.TryRemove(threadId, out _);
        }
    }

    /// <summary>
    /// Creates a local media session with audio and video socket settings
    /// Per Microsoft samples: Include VideoSocketSettings even when video is inactive
    /// </summary>
    private ILocalMediaSession CreateMediaSession(string? callId = null)
    {
        Guid mediaSessionId = default;
        if (!string.IsNullOrEmpty(callId) && Guid.TryParse(callId, out var parsedId))
        {
            mediaSessionId = parsedId;
        }

        return Client.CreateMediaSession(
            new AudioSocketSettings
            {
                StreamDirections = StreamDirection.Recvonly,  // We only need to receive audio
                SupportedAudioFormat = AudioFormat.Pcm16K,    // 16 kHz PCM per S5
                ReceiveUnmixedMeetingAudio = false            // Mixed audio is fine for transcription
            },
            new VideoSocketSettings
            {
                StreamDirections = StreamDirection.Inactive   // No video needed
            },
            mediaSessionId: mediaSessionId);
    }

    /// <summary>
    /// Join a Teams meeting using join URL
    /// Per S6: POST /communications/calls with meeting info
    /// 
    /// Note: CallHandler creation is now handled by OnCallsUpdated event
    /// This method just initiates the join; SDK events handle the rest
    /// </summary>
    public async Task<string> JoinMeetingAsync(
        string joinUrl,
        string displayName,
        AzureSpeechRealtimeTranscriber transcriber)
    {
        if (_client == null)
        {
            throw new InvalidOperationException("Bot service not initialized");
        }

        _logger.LogInformation("Joining meeting: {JoinUrl}, DisplayName: {DisplayName}", joinUrl, displayName);

        // A tracking id for logging purposes. Helps identify this call in logs.
        var scenarioId = Guid.NewGuid();

        // Parse join URL to extract meeting info per S7
        var (chatInfo, meetingInfo) = ParseJoinUrl(joinUrl);

        // Create media session with both audio and video settings
        var mediaSession = CreateMediaSession();

        var tenantId = meetingInfo.Organizer?.GetPrimaryIdentity()?.GetTenantId();
        var joinParams = new JoinMeetingParameters(chatInfo, meetingInfo, mediaSession)
        {
            TenantId = string.IsNullOrWhiteSpace(tenantId) ? _botConfig.TenantId : tenantId
        };

        // If display name is specified, join as guest
        if (!string.IsNullOrWhiteSpace(displayName))
        {
            joinParams.GuestIdentity = new Identity
            {
                Id = Guid.NewGuid().ToString(),
                DisplayName = displayName
            };
        }

        // Check if we already have a handler for this thread
        if (string.IsNullOrWhiteSpace(chatInfo.ThreadId))
        {
            throw new InvalidOperationException("Join URL did not contain a valid threadId");
        }
        var threadId = chatInfo.ThreadId;
        if (CallHandlers.ContainsKey(threadId))
        {
            _logger.LogWarning("Call handler already exists for thread: {ThreadId}", threadId);
            throw new InvalidOperationException($"Bot is already in a call for thread: {threadId}");
        }

        // Store transcriber for when CallHandler is created by OnCallsUpdated
        // We need to pre-register the transcriber since OnCallsUpdated won't have access to it
        // Note: This is a simplification - in production, you'd use a more robust pattern
        _pendingTranscribers[threadId] = transcriber;

        // Join the call - this will trigger OnCallsUpdated which creates the CallHandler
        var call = await _client.Calls().AddAsync(joinParams, scenarioId).ConfigureAwait(false);

        _logger.LogInformation("Call created: {CallId}, ThreadId: {ThreadId}", call.Id, threadId);

        return call.Id;
    }

    // Temporary storage for transcribers while CallHandler is being created
    private readonly ConcurrentDictionary<string, AzureSpeechRealtimeTranscriber> _pendingTranscribers = new();

    /// <summary>
    /// Gets or creates a transcriber for the given thread
    /// </summary>
    internal AzureSpeechRealtimeTranscriber GetOrCreateTranscriber(string threadId)
    {
        if (_pendingTranscribers.TryRemove(threadId, out var transcriber))
        {
            return transcriber;
        }

        // If no pending transcriber, create a new one via DI
        using var scope = _serviceProvider.CreateScope();
        return scope.ServiceProvider.GetRequiredService<AzureSpeechRealtimeTranscriber>();
    }

    /// <summary>
    /// Parse Teams join URL into ChatInfo and MeetingInfo
    /// Per S7: call resource requires chatInfo and meetingInfo
    /// </summary>
    private (ChatInfo chatInfo, OrganizerMeetingInfo meetingInfo) ParseJoinUrl(string joinUrl)
    {
        var decodedUrl = WebUtility.UrlDecode(joinUrl);
        var match = Regex.Match(
            decodedUrl,
            "https://teams\\.microsoft\\.com.*/(?<thread>[^/]+)/(?<message>[^/]+)\\?context=(?<context>{.*})");

        if (!match.Success)
        {
            throw new ArgumentException($"Join URL cannot be parsed: {joinUrl}.", nameof(joinUrl));
        }

        JoinUrlContext context;
        using (var stream = new MemoryStream(Encoding.UTF8.GetBytes(match.Groups["context"].Value)))
        {
            context = (JoinUrlContext)new DataContractJsonSerializer(typeof(JoinUrlContext)).ReadObject(stream)!;
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
    /// Gracefully shuts down the bot service, terminating all active calls
    /// </summary>
    public async Task ShutdownAsync()
    {
        _logger.LogWarning("Shutting down bot service - terminating all active calls");

        if (_client != null)
        {
            // Unsubscribe from events
            _client.Calls().OnIncoming -= OnCallsIncoming;
            _client.Calls().OnUpdated -= OnCallsUpdated;

            // Terminate all active calls
            await _client.TerminateAsync().ConfigureAwait(false);
        }

        // Dispose all handlers
        foreach (var kvp in CallHandlers)
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
        }
        CallHandlers.Clear();
        _pendingTranscribers.Clear();

        _logger.LogInformation("Bot service shutdown complete");
    }

    public async ValueTask DisposeAsync()
    {
        await ShutdownAsync().ConfigureAwait(false);
        
        if (_client != null)
        {
            _client.Dispose();
        }
    }
}

/// <summary>
/// Authentication provider for Graph Communications
/// Implements production-grade JWT validation and token caching
/// Based on Microsoft's official EchoBot sample
/// Sources: 
/// - https://github.com/microsoftgraph/microsoft-graph-comms-samples/blob/master/Samples/PublicSamples/EchoBot/src/EchoBot/Authentication/AuthenticationProvider.cs
/// - https://microsoftgraph.github.io/microsoft-graph-comms-samples/docs/articles/calls/calling-notifications.html
/// </summary>
internal class AuthenticationProvider : IRequestAuthenticationProvider
{
    private const string AuthDomain = "https://api.aps.skype.com/v1/.well-known/OpenIdConfiguration";
    private const string GraphResource = "https://graph.microsoft.com";
    
    private readonly string _clientId;
    private readonly string _clientSecret;
    private readonly string _defaultTenantId;
    private readonly ILogger _logger;
    
    // Singleton MSAL application for token caching
    private readonly IConfidentialClientApplication _msalApp;
    
    // OpenID configuration for JWT validation (refreshed every 2 hours)
    private readonly TimeSpan _openIdConfigRefreshInterval = TimeSpan.FromHours(2);
    private DateTime _prevOpenIdConfigUpdateTimestamp = DateTime.MinValue;
    private OpenIdConnectConfiguration? _openIdConfiguration;
    private readonly SemaphoreSlim _configLock = new(1, 1);

    public AuthenticationProvider(string clientId, string clientSecret, string tenantId, ILogger logger)
    {
        _clientId = clientId ?? throw new ArgumentNullException(nameof(clientId));
        _clientSecret = clientSecret ?? throw new ArgumentNullException(nameof(clientSecret));
        _defaultTenantId = tenantId ?? throw new ArgumentNullException(nameof(tenantId));
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
        
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
    /// Authenticates outbound requests to Microsoft Graph
    /// Uses cached tokens when available (MSAL handles caching automatically)
    /// Per Microsoft recommendation: Token caching improves performance and prevents throttling
    /// </summary>
    public async Task AuthenticateOutboundRequestAsync(HttpRequestMessage request, string tenant)
    {
        var scopes = new[] { $"{GraphResource}/.default" };
        var resolvedTenant = string.IsNullOrWhiteSpace(tenant) ? _defaultTenantId : tenant;
        if (string.IsNullOrWhiteSpace(resolvedTenant))
        {
            resolvedTenant = "common";
        }

        _logger.LogDebug("Acquiring token for tenant: {Tenant}", resolvedTenant);

        try
        {
            // If tenant differs from default, create temporary app for that tenant
            // Otherwise use singleton (which has cached tokens)
            IConfidentialClientApplication app;
            if (resolvedTenant != _defaultTenantId)
            {
                var authority = $"https://login.microsoftonline.com/{resolvedTenant}";
                app = ConfidentialClientApplicationBuilder
                    .Create(_clientId)
                    .WithClientSecret(_clientSecret)
                    .WithAuthority(authority)
                    .Build();
                _logger.LogDebug("Created temporary MSAL app for tenant: {Tenant}", resolvedTenant);
            }
            else
            {
                app = _msalApp;
            }

            // Acquire token with retry (MSAL automatically uses cache)
            var result = await AcquireTokenWithRetryAsync(app, scopes, 3);
            
            _logger.LogDebug(
                "Token acquired successfully. Expires in {Minutes:F1} minutes", 
                result.ExpiresOn.Subtract(DateTimeOffset.UtcNow).TotalMinutes);

            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", result.AccessToken);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to acquire token for client: {ClientId}, tenant: {Tenant}", _clientId, resolvedTenant);
            throw;
        }
    }

    /// <summary>
    /// Validates inbound requests from Microsoft Graph
    /// Implements production-grade JWT validation per Microsoft security requirements
    /// 
    /// Security checks:
    /// 1. Validates JWT signature using OpenID Connect configuration
    /// 2. Verifies issuers: https://graph.microsoft.com and https://api.botframework.com
    /// 3. Validates audience matches App ID
    /// 4. Extracts tenant ID from token claims
    /// 
    /// Per Microsoft: Returning IsValid=false triggers 403 Forbidden response
    /// Source: https://microsoftgraph.github.io/microsoft-graph-comms-samples/docs/client/Microsoft.Graph.Communications.Client.Authentication.IRequestAuthenticationProvider.html
    /// </summary>
    public async Task<RequestValidationResult> ValidateInboundRequestAsync(HttpRequestMessage request)
    {
        var token = request?.Headers?.Authorization?.Parameter;
        if (string.IsNullOrWhiteSpace(token))
        {
            _logger.LogWarning("Inbound request validation failed: No Authorization token provided");
            return new RequestValidationResult { IsValid = false };
        }

        // Update OpenID configuration if needed (cached for 2 hours)
        if (_openIdConfiguration == null || DateTime.Now > _prevOpenIdConfigUpdateTimestamp.Add(_openIdConfigRefreshInterval))
        {
            await _configLock.WaitAsync();
            try
            {
                // Double-check after acquiring lock
                if (_openIdConfiguration == null || DateTime.Now > _prevOpenIdConfigUpdateTimestamp.Add(_openIdConfigRefreshInterval))
                {
                    _logger.LogInformation("Updating OpenID configuration from {AuthDomain}", AuthDomain);

                    // Download the OIDC configuration which contains the JWKS
                    // Microsoft signs tokens with private certificates; we validate with public keys
                    IConfigurationManager<OpenIdConnectConfiguration> configurationManager =
                        new ConfigurationManager<OpenIdConnectConfiguration>(
                            AuthDomain,
                            new OpenIdConnectConfigurationRetriever());
                    
                    _openIdConfiguration = await configurationManager.GetConfigurationAsync(CancellationToken.None);
                    _prevOpenIdConfigUpdateTimestamp = DateTime.Now;
                    
                    _logger.LogInformation(
                        "OpenID configuration updated. {KeyCount} signing keys available", 
                        _openIdConfiguration.SigningKeys.Count);
                }
            }
            finally
            {
                _configLock.Release();
            }
        }

        // Validate issuers: Graph and Bot Framework
        var authIssuers = new[]
        {
            "https://graph.microsoft.com",
            "https://api.botframework.com",
        };

        // Configure token validation parameters
        var validationParameters = new TokenValidationParameters
        {
            ValidIssuers = authIssuers,
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
        var tenantClaim = claimsPrincipal.FindFirst(claim => claim.Type.Equals(TenantIdClaimType, StringComparison.Ordinal));

        if (string.IsNullOrEmpty(tenantClaim?.Value))
        {
            _logger.LogWarning("Inbound request validation failed: No tenant claim in token");
            return new RequestValidationResult { IsValid = false };
        }

        _logger.LogDebug("Request validated successfully for tenant: {TenantId}", tenantClaim.Value);
        
        // Store tenant in request options for SDK use (HttpRequestMessage.Properties is obsolete)
        request.Options.Set(new HttpRequestOptionsKey<string>("Microsoft-Tenant-Id"), tenantClaim.Value);
        
        return new RequestValidationResult 
        { 
            IsValid = true, 
            TenantId = tenantClaim.Value 
        };
    }

    /// <summary>
    /// Acquires token with retry logic for transient failures
    /// MSAL automatically uses cached tokens when available
    /// </summary>
    private async Task<AuthenticationResult> AcquireTokenWithRetryAsync(
        IConfidentialClientApplication app, 
        string[] scopes, 
        int attempts)
    {
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
            catch (Exception ex)
            {
                if (attempts < 1)
                {
                    _logger.LogError(ex, "Token acquisition failed after all retry attempts");
                    throw;
                }
                
                _logger.LogWarning(ex, "Token acquisition failed. Retrying... ({AttemptsLeft} attempts left)", attempts);
            }

            await Task.Delay(1000).ConfigureAwait(false);
        }
    }
}

[DataContract]
internal sealed class JoinUrlContext
{
    [DataMember]
    public string Tid { get; set; } = string.Empty;

    [DataMember]
    public string Oid { get; set; } = string.Empty;

    [DataMember]
    public string MessageId { get; set; } = string.Empty;
}
