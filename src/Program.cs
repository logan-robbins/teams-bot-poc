// ==============================================
// Talestral Teams Media Bot - Program Entry Point
// ==============================================
// ASP.NET Core 8.0 application for Teams meeting transcription.
// Receives real-time audio via Graph Communications SDK and 
// streams it to STT providers (Deepgram/Azure Speech).
// ==============================================

using Microsoft.Graph.Communications.Common.Telemetry;
using Serilog;
using System.Security.Cryptography.X509Certificates;
using TeamsMediaBot.Models;
using TeamsMediaBot.Services;

var builder = WebApplication.CreateBuilder(args);

// Load configuration from Config/appsettings.json (required for VM/service deployment)
builder.Configuration.AddJsonFile("Config/appsettings.json", optional: false, reloadOnChange: true);

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
    // Load and validate configuration sections
    var botConfig = LoadRequiredConfiguration<BotConfiguration>(builder.Configuration, "Bot");
    var mediaConfig = LoadRequiredConfiguration<MediaPlatformConfiguration>(builder.Configuration, "MediaPlatformSettings");
    var sttConfig = LoadSttConfiguration(builder.Configuration);
    var transcriptSinkConfig = LoadRequiredConfiguration<TranscriptSinkConfiguration>(builder.Configuration, "TranscriptSink");

    // Configure Kestrel to listen on the specified URL with TLS if configured
    ConfigureKestrel(builder, botConfig, mediaConfig);

    // Register configuration as singletons
    builder.Services.AddSingleton(botConfig);
    builder.Services.AddSingleton(mediaConfig);
    builder.Services.AddSingleton(sttConfig);
    builder.Services.AddSingleton(transcriptSinkConfig);

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
        return new TranscriberFactory(
            sttConfig,
            transcriptSinkConfig.PythonEndpoint,
            loggerFactory);
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
        "Teams Media Bot starting - ListenUrl={ListenUrl}, NotificationUrl={NotificationUrl}, MediaEndpoint={ServiceFqdn}:{PublicPort}",
        botConfig.LocalHttpListenUrl,
        botConfig.NotificationUrl,
        mediaConfig.ServiceFqdn,
        mediaConfig.InstancePublicPort);

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
/// Configures Kestrel to listen on the specified URL with optional TLS.
/// </summary>
static void ConfigureKestrel(
    WebApplicationBuilder builder, 
    BotConfiguration botConfig, 
    MediaPlatformConfiguration mediaConfig)
{
    var listenUri = new Uri(botConfig.LocalHttpListenUrl);
    
    // Validate HTTP/HTTPS port configuration
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
                var cert = LoadCertificateFromStore(mediaConfig.CertificateThumbprint);
                listenOptions.UseHttps(cert);
            }
        });
    });
}

/// <summary>
/// Loads an X509 certificate from the LocalMachine certificate store by thumbprint.
/// </summary>
static X509Certificate2 LoadCertificateFromStore(string thumbprint)
{
    ArgumentException.ThrowIfNullOrWhiteSpace(thumbprint);
    
    if (string.Equals(thumbprint, "CHANGE_AFTER_CERT_INSTALL", StringComparison.OrdinalIgnoreCase))
    {
        throw new InvalidOperationException(
            "CertificateThumbprint is not set. Update Config/appsettings.json with the actual certificate thumbprint.");
    }

    var normalizedThumbprint = thumbprint
        .Replace(" ", string.Empty, StringComparison.Ordinal)
        .ToUpperInvariant();
    
    using var store = new X509Store(StoreName.My, StoreLocation.LocalMachine);
    store.Open(OpenFlags.ReadOnly);

    var certificates = store.Certificates.Find(
        X509FindType.FindByThumbprint,
        normalizedThumbprint,
        validOnly: false);

    if (certificates.Count == 0)
    {
        throw new InvalidOperationException(
            $"Certificate with thumbprint '{normalizedThumbprint}' not found in LocalMachine/My store.");
    }

    return certificates[0];
}
