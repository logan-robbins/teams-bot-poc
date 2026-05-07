// ==============================================
// Alfred Teams Media Bot - Program Entry Point
// ==============================================
// ASP.NET Core 8.0 application for Teams meeting transcription.
// Receives real-time audio via Graph Communications SDK and 
// streams it to STT providers (Deepgram/Azure Speech).
// ==============================================

using Microsoft.Bot.Builder;
using Microsoft.Bot.Builder.Integration.AspNet.Core;
using Microsoft.Bot.Connector.Authentication;
using Microsoft.Graph.Communications.Common.Telemetry;
using Serilog;
using System.Security.Cryptography.X509Certificates;
using TeamsMediaBot.Models;
using TeamsMediaBot.Services;

var configResolution = ResolveConfiguration(args);

var builder = WebApplication.CreateBuilder(args);

// Load configuration from explicit path (CLI/env/default fallback)
builder.Configuration.Sources.Clear();
builder.Configuration.AddJsonFile(configResolution.ConfigPath, optional: false, reloadOnChange: true);
builder.Configuration.AddEnvironmentVariables();
builder.Configuration.AddCommandLine(args);

// Configure Serilog for structured logging
Log.Logger = new LoggerConfiguration()
    .ReadFrom.Configuration(builder.Configuration)
    .Enrich.FromLogContext()
    .Enrich.WithMachineName()
    .Enrich.WithThreadId()
    .WriteTo.Console(outputTemplate: "[{Timestamp:HH:mm:ss} {Level:u3}] {Message:lj}{NewLine}{Exception}")
    .WriteTo.File("logs/teamsbot-.log", rollingInterval: RollingInterval.Day, retainedFileCountLimit: 14)
    .CreateLogger();

builder.Host.UseSerilog();

try
{
    Log.Information(
        "Loaded configuration: Path={ConfigPath}, Source={ConfigSource}, InstanceId={InstanceId}",
        configResolution.ConfigPath,
        configResolution.Source,
        configResolution.InstanceId);

    // Load and validate configuration sections
    var botConfig = LoadRequiredConfiguration<BotConfiguration>(builder.Configuration, "Bot");
    var mediaConfigRaw = LoadRequiredConfiguration<MediaPlatformConfiguration>(builder.Configuration, "MediaPlatformSettings");
    var sttConfig = LoadSttConfiguration(builder.Configuration);
    var transcriptSinkConfig = LoadRequiredConfiguration<TranscriptSinkConfiguration>(builder.Configuration, "TranscriptSink");
    ValidateTranscriptSinkConfiguration(transcriptSinkConfig);
    var joinModeSettings = builder.Configuration.GetSection("JoinMode").Get<JoinModeSettings>()
        ?? new JoinModeSettings();
    var meetingChatConfig = builder.Configuration.GetSection("MeetingChat").Get<MeetingChatConfiguration>()
        ?? new MeetingChatConfiguration();

    // Resolve the TLS cert ONCE at startup. Falls back to Subject-CN/FriendlyName
    // matching when the configured thumbprint is missing/stale (e.g. after auto
    // cert renewal). Downstream consumers see the actual resolved thumbprint.
    var resolvedCert = ResolveCertificate(mediaConfigRaw);
    Log.Information(
        "Resolved TLS certificate: Subject={Subject}, Thumbprint={Thumbprint}, NotAfter={NotAfter}",
        resolvedCert.Subject, resolvedCert.Thumbprint, resolvedCert.NotAfter);
    var mediaConfig = new MediaPlatformConfiguration
    {
        ApplicationId = mediaConfigRaw.ApplicationId,
        CertificateThumbprint = resolvedCert.Thumbprint,
        CertificateFriendlyName = mediaConfigRaw.CertificateFriendlyName,
        InstanceInternalPort = mediaConfigRaw.InstanceInternalPort,
        InstancePublicPort = mediaConfigRaw.InstancePublicPort,
        ServiceFqdn = mediaConfigRaw.ServiceFqdn,
        InstancePublicIPAddress = mediaConfigRaw.InstancePublicIPAddress,
    };

    // Configure Kestrel to listen on the specified URL with TLS if configured
    ConfigureKestrel(builder, botConfig, resolvedCert);

    // Register configuration as singletons
    builder.Services.AddSingleton(botConfig);
    builder.Services.AddSingleton(mediaConfig);
    builder.Services.AddSingleton(sttConfig);
    builder.Services.AddSingleton(transcriptSinkConfig);
    builder.Services.AddSingleton(joinModeSettings);
    builder.Services.AddSingleton(meetingChatConfig);

    // Bot Framework adapter for Alfred's proactive-messaging chat send path.
    // Graph has no application-permission alternative for posting chat in 2026,
    // so this is the only compliant route.
    builder.Services.AddSingleton<BotFrameworkAuthentication>(sp =>
        new ConfigurationBotFrameworkAuthentication(sp.GetRequiredService<IConfiguration>()));
    // CloudAdapter has two public ctors that DI can't disambiguate
    // (IConfiguration/IHttpClientFactory/ILogger vs BotFrameworkAuthentication/ILogger)
    // so we pick the auth-based ctor explicitly.
    builder.Services.AddSingleton<IBotFrameworkHttpAdapter>(sp =>
        new CloudAdapter(
            sp.GetRequiredService<BotFrameworkAuthentication>(),
            sp.GetRequiredService<ILogger<IBotFrameworkHttpAdapter>>()));
    builder.Services.AddSingleton<IConversationReferenceStore, InMemoryConversationReferenceStore>();
    builder.Services.AddSingleton<IBot, AlfredBot>();

    // Per-meeting raw event audit log (NDJSON files keyed by sanitized chat_thread_id).
    var auditLogDir = Path.Combine(
        builder.Configuration["MeetingAuditLogDir"] ?? @"C:\teams-bot-poc\meeting-logs");
    builder.Services.AddSingleton(new MeetingAuditLogger(auditLogDir));

    // Meeting-chat services (inbound chat → Python sink; Graph subscription lifecycle).
    builder.Services.AddSingleton<IMeetingChatService, MeetingChatService>();
    builder.Services.AddHttpClient<PythonChatPublisher>();
    builder.Services.AddHttpClient<ChannelLinkPublisher>();
    builder.Services.AddHttpClient<GraphApiClient>();
    builder.Services.AddSingleton<GraphNotificationCrypto>();
    builder.Services.AddSingleton<GraphValidationTokenValidator>();
    builder.Services.AddSingleton<GraphNotificationProcessor>();

    // Persistent channel attachments (channel-level analog of "the bot is in
    // this meeting"). Re-issues subscriptions for each persisted channel on
    // startup so attachment is genuinely durable across bot restarts.
    builder.Services.AddSingleton(new ChannelAttachmentStoreOptions
    {
        FilePath = meetingChatConfig.ChannelAttachmentStorePath,
    });
    builder.Services.AddSingleton<ChannelAttachmentStore>();
    builder.Services.AddSingleton<ChannelAttachmentService>();
    builder.Services.AddSingleton<IChannelAttachmentService>(sp =>
        sp.GetRequiredService<ChannelAttachmentService>());
    builder.Services.AddHostedService(sp =>
        sp.GetRequiredService<ChannelAttachmentService>());

    // Register Graph Communications logger
    builder.Services.AddSingleton<IGraphLogger>(sp =>
    {
        var loggerFactory = sp.GetRequiredService<ILoggerFactory>();
        var graphLogger = new GraphLogger(
            component: "TeamsMediaBotPOC",
            properties: Array.Empty<object>(),
            redirectToTrace: false,
            obfuscationConfiguration: null);
        graphLogger.BindToILoggerFactory(loggerFactory);
        return graphLogger;
    });

    // Register transcriber factory (creates transcribers outside of DI tracking)
    builder.Services.AddSingleton<TranscriberFactory>(sp =>
    {
        var loggerFactory = sp.GetRequiredService<ILoggerFactory>();
        var meetingAuditLogger = sp.GetRequiredService<MeetingAuditLogger>();
        return new TranscriberFactory(
            sttConfig,
            transcriptSinkConfig.PythonEndpoint,
            loggerFactory,
            meetingAuditLogger);
    });

    // Register bot service as singleton
    builder.Services.AddSingleton<TeamsCallingBotService>();

    // Add controllers with Newtonsoft.Json (required by Graph Communications SDK)
    builder.Services.AddControllers()
        .AddNewtonsoftJson();

    // Add health checks for monitoring
    builder.Services.AddHealthChecks();

    var app = builder.Build();

    // Configure HTTP request pipeline
    app.UseRouting();
    app.MapControllers();
    app.MapHealthChecks("/health");

    // Initialize bot service on startup
    var botService = app.Services.GetRequiredService<TeamsCallingBotService>();
    await botService.InitializeAsync().ConfigureAwait(false);

    Log.Information(
        "Teams Media Bot starting - InstanceId={InstanceId}, ListenUrl={ListenUrl}, NotificationUrl={NotificationUrl}, MediaEndpoint={ServiceFqdn}:{PublicPort}, SinkEndpoint={SinkEndpoint}",
        configResolution.InstanceId,
        botConfig.LocalHttpListenUrl,
        botConfig.NotificationUrl,
        mediaConfig.ServiceFqdn,
        mediaConfig.InstancePublicPort,
        transcriptSinkConfig.PythonEndpoint);

    await app.RunAsync().ConfigureAwait(false);

    // Graceful shutdown
    Log.Information("Shutting down bot service...");
    await botService.DisposeAsync().ConfigureAwait(false);
}
catch (Exception ex)
{
    Log.Fatal(ex, "Application terminated unexpectedly");
    throw;
}
finally
{
    await Log.CloseAndFlushAsync().ConfigureAwait(false);
}

return;

// ==============================================
// Local Helper Methods
// ==============================================

/// <summary>
/// Loads a required configuration section, throwing if missing.
/// </summary>
static T LoadRequiredConfiguration<T>(IConfiguration configuration, string sectionName) where T : class
{
    return configuration.GetSection(sectionName).Get<T>()
        ?? throw new InvalidOperationException($"Configuration section '{sectionName}' is missing or invalid.");
}

/// <summary>
/// Validates endpoints required by the single Teams-chat ingress path.
/// </summary>
static void ValidateTranscriptSinkConfiguration(TranscriptSinkConfiguration config)
{
    ArgumentNullException.ThrowIfNull(config);
    if (string.IsNullOrWhiteSpace(config.PythonEndpoint))
    {
        throw new InvalidOperationException(
            "TranscriptSink.PythonEndpoint is required for transcript forwarding.");
    }

    if (string.IsNullOrWhiteSpace(config.ChatEndpoint))
    {
        throw new InvalidOperationException(
            "TranscriptSink.ChatEndpoint is required for inbound Teams chat forwarding.");
    }
}

/// <summary>
/// Resolves configuration file path and instance identity with strict precedence.
/// </summary>
static ConfigResolution ResolveConfiguration(string[] args)
{
    ArgumentNullException.ThrowIfNull(args);

    var fromArgs = TryGetConfigPathFromArgs(args);
    if (!string.IsNullOrWhiteSpace(fromArgs))
    {
        return BuildConfigResolution(fromArgs, "command-line");
    }

    var fromEnv = Environment.GetEnvironmentVariable("ALFRED_CONFIG_PATH");
    if (!string.IsNullOrWhiteSpace(fromEnv))
    {
        return BuildConfigResolution(fromEnv, "environment");
    }

    return BuildConfigResolution("Config/appsettings.json", "default");
}

/// <summary>
/// Parses supported command-line forms:
/// --config /path/to/file.json
/// --config=/path/to/file.json
/// </summary>
static string? TryGetConfigPathFromArgs(string[] args)
{
    for (var i = 0; i < args.Length; i++)
    {
        var arg = args[i];
        if (string.Equals(arg, "--config", StringComparison.Ordinal))
        {
            if (i + 1 >= args.Length || string.IsNullOrWhiteSpace(args[i + 1]))
            {
                throw new InvalidOperationException(
                    "Command-line option '--config' requires a non-empty file path argument.");
            }

            return args[i + 1];
        }

        const string prefix = "--config=";
        if (arg.StartsWith(prefix, StringComparison.Ordinal))
        {
            var value = arg[prefix.Length..];
            if (string.IsNullOrWhiteSpace(value))
            {
                throw new InvalidOperationException(
                    "Command-line option '--config=' requires a non-empty file path value.");
            }

            return value;
        }
    }

    return null;
}

/// <summary>
/// Validates the configuration path and derives instance metadata.
/// </summary>
static ConfigResolution BuildConfigResolution(string rawPath, string source)
{
    if (string.IsNullOrWhiteSpace(rawPath))
    {
        throw new InvalidOperationException(
            $"Configuration path from {source} is empty. Provide a valid appsettings JSON path.");
    }

    var fullPath = Path.GetFullPath(rawPath);
    if (!File.Exists(fullPath))
    {
        throw new InvalidOperationException(
            $"Configuration file not found at '{fullPath}'. " +
            "Set a valid path via '--config <path>' or ALFRED_CONFIG_PATH.");
    }

    var fileName = Path.GetFileNameWithoutExtension(fullPath);
    var instanceId = DeriveInstanceId(fileName);
    return new ConfigResolution(fullPath, source, instanceId);
}

/// <summary>
/// Maps configuration file naming to instance identity.
/// Example: appsettings.meeting-a.json -> meeting-a
/// </summary>
static string DeriveInstanceId(string fileNameWithoutExtension)
{
    if (string.IsNullOrWhiteSpace(fileNameWithoutExtension))
    {
        return "default";
    }

    var candidate = fileNameWithoutExtension;
    const string appSettingsPrefix = "appsettings.";
    if (candidate.StartsWith(appSettingsPrefix, StringComparison.OrdinalIgnoreCase))
    {
        candidate = candidate[appSettingsPrefix.Length..];
    }
    else if (string.Equals(candidate, "appsettings", StringComparison.OrdinalIgnoreCase))
    {
        candidate = "default";
    }

    var filtered = new string(candidate
        .Trim()
        .Where(ch => char.IsLetterOrDigit(ch) || ch == '-' || ch == '_')
        .ToArray());

    return string.IsNullOrWhiteSpace(filtered) ? "default" : filtered;
}

/// <summary>
/// Loads STT configuration with backward compatibility for legacy 'Speech' section.
/// </summary>
static SttConfiguration LoadSttConfiguration(IConfiguration configuration)
{
    var sttConfig = configuration.GetSection("Stt").Get<SttConfiguration>();
    
    if (sttConfig is null)
    {
        // Fall back to legacy 'Speech' section
        var speechConfig = configuration.GetSection("Speech").Get<SpeechConfiguration>()
            ?? throw new InvalidOperationException(
                "STT configuration is missing. Provide either 'Stt' or legacy 'Speech' section.");

        return new SttConfiguration
        {
            Provider = "AzureSpeech",
            AzureSpeech = new AzureSpeechProviderConfiguration
            {
                Key = speechConfig.Key,
                Region = speechConfig.Region,
                RecognitionLanguage = speechConfig.RecognitionLanguage,
                EndpointId = null
            }
        };
    }

    // If Stt.Provider selects AzureSpeech but Stt.AzureSpeech is missing, 
    // fall back to legacy Speech section for credentials
    var provider = (sttConfig.Provider ?? string.Empty).Trim();
    var isAzureProvider = provider.Equals("AzureSpeech", StringComparison.OrdinalIgnoreCase) ||
                          provider.Equals("Azure", StringComparison.OrdinalIgnoreCase);
    
    if (isAzureProvider && sttConfig.AzureSpeech is null)
    {
        var speechConfig = configuration.GetSection("Speech").Get<SpeechConfiguration>()
            ?? throw new InvalidOperationException(
                "Stt.Provider='AzureSpeech' but no Stt.AzureSpeech and no legacy Speech section found.");

        sttConfig.AzureSpeech = new AzureSpeechProviderConfiguration
        {
            Key = speechConfig.Key,
            Region = speechConfig.Region,
            RecognitionLanguage = speechConfig.RecognitionLanguage,
            EndpointId = null
        };
    }

    return sttConfig;
}

/// <summary>
/// Configures Kestrel to listen on the specified URL, using a pre-resolved cert for TLS.
/// </summary>
static void ConfigureKestrel(
    WebApplicationBuilder builder,
    BotConfiguration botConfig,
    X509Certificate2 tlsCert)
{
    var listenUri = new Uri(botConfig.LocalHttpListenUrl);

    if (string.Equals(listenUri.Scheme, "http", StringComparison.OrdinalIgnoreCase) && listenUri.Port == 443)
    {
        throw new InvalidOperationException(
            "LocalHttpListenUrl is HTTP on port 443. Use https:// for TLS or change the port.");
    }

    builder.WebHost.ConfigureKestrel(options =>
    {
        options.ListenAnyIP(listenUri.Port, listenOptions =>
        {
            if (string.Equals(listenUri.Scheme, "https", StringComparison.OrdinalIgnoreCase))
            {
                listenOptions.UseHttps(tlsCert);
            }
        });
    });
}

/// <summary>
/// Resolves the TLS certificate from LocalMachine/My, tolerant to cert auto-renewal.
/// Resolution order: configured thumbprint → Subject CN matches ServiceFqdn →
/// FriendlyName starts with the configured prefix. Latest NotAfter wins on
/// fallback. Throws if no match.
/// </summary>
static X509Certificate2 ResolveCertificate(MediaPlatformConfiguration mediaConfig)
{
    ArgumentNullException.ThrowIfNull(mediaConfig);

    using var store = new X509Store(StoreName.My, StoreLocation.LocalMachine);
    store.Open(OpenFlags.ReadOnly);

    var thumbprint = mediaConfig.CertificateThumbprint;
    var hasUsableThumbprint = !string.IsNullOrWhiteSpace(thumbprint)
        && !string.Equals(thumbprint, "CHANGE_AFTER_CERT_INSTALL", StringComparison.OrdinalIgnoreCase);

    if (hasUsableThumbprint)
    {
        var normalized = thumbprint
            .Replace(" ", string.Empty, StringComparison.Ordinal)
            .ToUpperInvariant();
        var byThumb = store.Certificates.Find(X509FindType.FindByThumbprint, normalized, validOnly: false);
        if (byThumb.Count > 0)
        {
            return byThumb[0];
        }
    }

    var fqdn = mediaConfig.ServiceFqdn;
    var friendlyPrefix = mediaConfig.CertificateFriendlyName;

    var candidates = store.Certificates.Cast<X509Certificate2>().ToList();

    if (!string.IsNullOrWhiteSpace(fqdn))
    {
        var bySubject = candidates
            .Where(c =>
                c.Subject.Contains($"CN={fqdn}", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(c.GetNameInfo(X509NameType.DnsName, false), fqdn, StringComparison.OrdinalIgnoreCase))
            .OrderByDescending(c => c.NotAfter)
            .ToList();
        if (bySubject.Count > 0)
        {
            return bySubject[0];
        }
    }

    if (!string.IsNullOrWhiteSpace(friendlyPrefix))
    {
        var byFriendly = candidates
            .Where(c => !string.IsNullOrWhiteSpace(c.FriendlyName) &&
                        c.FriendlyName.StartsWith(friendlyPrefix, StringComparison.OrdinalIgnoreCase))
            .OrderByDescending(c => c.NotAfter)
            .ToList();
        if (byFriendly.Count > 0)
        {
            return byFriendly[0];
        }
    }

    throw new InvalidOperationException(
        $"Could not resolve a TLS certificate from LocalMachine/My. " +
        $"Tried Thumbprint='{thumbprint}', ServiceFqdn='{fqdn}', FriendlyNamePrefix='{friendlyPrefix}'. " +
        $"Install a certificate with CN={fqdn} or set MediaPlatformSettings.CertificateThumbprint/CertificateFriendlyName.");
}

/// <summary>
/// Runtime configuration file selection metadata.
/// </summary>
file sealed record ConfigResolution(string ConfigPath, string Source, string InstanceId);
