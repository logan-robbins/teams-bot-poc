using Microsoft.Graph.Communications.Calls;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Graph.Communications.Client;
using Microsoft.Graph.Communications.Client.Authentication;
using Microsoft.Graph.Communications.Common;
using Microsoft.Graph.Communications.Common.Telemetry;
using Microsoft.Graph.Models;
using Microsoft.Identity.Client;
using System.Security.Cryptography.X509Certificates;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Core bot service for joining Teams meetings and receiving real-time audio
/// Based on Part A of the validated guide
/// Sources: S3, S4, S5, S6, S7, S10, S11, S14
/// </summary>
public class TeamsCallingBotService : IAsyncDisposable
{
    private readonly BotConfiguration _botConfig;
    private readonly MediaPlatformConfiguration _mediaConfig;
    private readonly ILogger<TeamsCallingBotService> _logger;
    private readonly IGraphLogger _graphLogger;

    private ICommunicationsClient? _client;
    private readonly Dictionary<string, CallContext> _activeCalls = new();

    public TeamsCallingBotService(
        BotConfiguration botConfig,
        MediaPlatformConfiguration mediaConfig,
        ILogger<TeamsCallingBotService> logger,
        IGraphLogger graphLogger)
    {
        _botConfig = botConfig;
        _mediaConfig = mediaConfig;
        _logger = logger;
        _graphLogger = graphLogger;
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
            _logger);

        // Configure media platform settings per S2, S14
        var mediaPlatformSettings = new MediaPlatformSettings
        {
            ApplicationId = _mediaConfig.ApplicationId,
            MediaPlatformInstanceSettings = new MediaPlatformInstanceSettings
            {
                CertificateThumbprint = _mediaConfig.CertificateThumbprint,
                InstanceInternalPort = _mediaConfig.InstanceInternalPort,
                InstancePublicPort = _mediaConfig.InstancePublicPort,
                InstancePublicIPAddress = System.Net.IPAddress.Parse(_mediaConfig.InstancePublicIPAddress),
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

        // Start the client
        await _client.StartAsync().ConfigureAwait(false);

        _logger.LogInformation("Teams Calling Bot Service initialized successfully");
    }

    /// <summary>
    /// Join a Teams meeting using join URL
    /// Per S6: POST /communications/calls with meeting info
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

        // Parse join URL to extract meeting info per S7
        var (chatInfo, meetingInfo) = ParseJoinUrl(joinUrl);

        // Create call with app-hosted media config per S6, S14
        var mediaSession = _client.CreateMediaSession(
            new AudioSocketSettings
            {
                StreamDirections = StreamDirection.Recvonly,  // We only need to receive audio
                SupportedAudioFormat = AudioFormat.Pcm16K,    // 16 kHz PCM per S5
                ReceiveUnmixedMeetingAudio = false            // Mixed audio is fine for transcription
            });

        var joinParams = new JoinMeetingParameters
        {
            ChatInfo = chatInfo,
            MeetingInfo = meetingInfo,
            MediaSession = mediaSession,
            TenantId = _botConfig.TenantId
        };

        // Join the call
        var call = await _client.Calls().AddAsync(joinParams).ConfigureAwait(false);

        _logger.LogInformation("Call created: {CallId}", call.Id);

        // Store call context
        var context = new CallContext
        {
            Call = call,
            MediaSession = mediaSession,
            Transcriber = transcriber,
            JoinedAt = DateTime.UtcNow
        };

        _activeCalls[call.Id] = context;

        // Wire up call state change handler
        call.OnUpdated += (sender, args) =>
        {
            _logger.LogInformation("Call state changed: {State}", call.Resource.State);

            if (call.Resource.State == CallState.Established)
            {
                _logger.LogInformation("Call established - media should start flowing");
                // Start transcription when call is established
                _ = Task.Run(async () =>
                {
                    try
                    {
                        await transcriber.StartAsync();
                    }
                    catch (Exception ex)
                    {
                        _logger.LogError(ex, "Failed to start transcriber");
                    }
                });
            }
            else if (call.Resource.State == CallState.Terminated)
            {
                _logger.LogInformation("Call terminated");
                _ = Task.Run(async () =>
                {
                    try
                    {
                        await transcriber.StopAsync();
                    }
                    catch (Exception ex)
                    {
                        _logger.LogError(ex, "Failed to stop transcriber");
                    }
                });

                _activeCalls.Remove(call.Id);
            }
        };

        // Wire up audio receive handler per S5
        if (mediaSession.AudioSocket != null)
        {
            mediaSession.AudioSocket.AudioMediaReceived += (sender, e) =>
            {
                // Per S5: Audio frames are 20ms each, delivered at ~50 fps
                // Extract PCM bytes from unmanaged buffer
                var buffer = e.Buffer;
                if (buffer != null && buffer.Data != null)
                {
                    // Convert unmanaged buffer to managed byte array
                    var pcmData = new byte[buffer.Length];
                    unsafe
                    {
                        fixed (byte* ptr = pcmData)
                        {
                            Buffer.MemoryCopy(
                                buffer.Data.ToPointer(),
                                ptr,
                                buffer.Length,
                                buffer.Length);
                        }
                    }

                    // Push to transcriber per J3
                    transcriber.PushPcm16k16bitMono(pcmData);
                }

                // Must dispose buffer per SDK requirements
                e.Buffer?.Dispose();
            };

            _logger.LogInformation("Audio media receive handler configured");
        }
        else
        {
            _logger.LogWarning("AudioSocket is null - media may not work");
        }

        return call.Id;
    }

    /// <summary>
    /// Parse Teams join URL into ChatInfo and MeetingInfo
    /// Per S7: call resource requires chatInfo and meetingInfo
    /// </summary>
    private (ChatInfo chatInfo, OrganizerMeetingInfo meetingInfo) ParseJoinUrl(string joinUrl)
    {
        // Teams join URLs contain thread ID and message ID
        // Format: https://teams.microsoft.com/l/meetup-join/19:meeting_XXXX@thread.v2/...
        
        var uri = new Uri(joinUrl);
        var threadId = ExtractThreadId(joinUrl);

        var chatInfo = new ChatInfo
        {
            ThreadId = threadId,
            MessageId = "0"  // Can be 0 for meetings
        };

        var meetingInfo = new OrganizerMeetingInfo
        {
            Organizer = new IdentitySet
            {
                User = new Identity
                {
                    Id = _botConfig.AppId,
                    TenantId = _botConfig.TenantId
                }
            },
            AllowConversationWithoutHost = true
        };

        return (chatInfo, meetingInfo);
    }

    private string ExtractThreadId(string joinUrl)
    {
        // Extract thread ID from join URL
        // Format: .../19:meeting_XXXXX@thread.v2/...
        var match = System.Text.RegularExpressions.Regex.Match(joinUrl, @"19:[^/]+@thread\.v2");
        if (match.Success)
        {
            return match.Value;
        }

        // Fallback: for scheduled meetings, thread ID might be in different format
        match = System.Text.RegularExpressions.Regex.Match(joinUrl, @"19:[^/]+");
        if (match.Success)
        {
            return match.Value + "@thread.v2";
        }

        throw new ArgumentException($"Could not extract thread ID from join URL: {joinUrl}");
    }

    public async ValueTask DisposeAsync()
    {
        if (_client != null)
        {
            await _client.DisposeAsync();
        }
    }

    private class CallContext
    {
        public required ICall Call { get; set; }
        public required IMediaSession MediaSession { get; set; }
        public required AzureSpeechRealtimeTranscriber Transcriber { get; set; }
        public DateTime JoinedAt { get; set; }
    }
}

/// <summary>
/// Authentication provider for Graph Communications
/// Per S13: Uses client credentials flow
/// </summary>
internal class AuthenticationProvider : IRequestAuthenticationProvider
{
    private readonly string _clientId;
    private readonly string _clientSecret;
    private readonly ILogger _logger;
    private readonly IConfidentialClientApplication _app;

    public AuthenticationProvider(string clientId, string clientSecret, ILogger logger)
    {
        _clientId = clientId;
        _clientSecret = clientSecret;
        _logger = logger;

        _app = ConfidentialClientApplicationBuilder
            .Create(_clientId)
            .WithClientSecret(_clientSecret)
            .WithAuthority("https://login.microsoftonline.com/common")
            .Build();
    }

    public async Task<string> AuthenticateOutboundRequestAsync(Uri requestUri, string tenantId)
    {
        var scopes = new[] { "https://graph.microsoft.com/.default" };

        try
        {
            var result = await _app
                .AcquireTokenForClient(scopes)
                .ExecuteAsync();

            return result.AccessToken;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to acquire token");
            throw;
        }
    }

    public Task AuthenticateInboundRequestAsync(HttpRequestMessage request)
    {
        // Inbound webhook validation would go here if needed
        return Task.CompletedTask;
    }
}
