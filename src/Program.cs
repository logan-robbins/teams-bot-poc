using Microsoft.Graph.Communications.Common.Telemetry;
using Microsoft.Extensions.Logging;
using Serilog;
using System.Security.Cryptography.X509Certificates;
using TeamsMediaBot.Models;
using TeamsMediaBot.Services;

var builder = WebApplication.CreateBuilder(args);

// Load configuration from Config/appsettings.json (required for VM/service)
builder.Configuration.AddJsonFile("Config/appsettings.json", optional: false, reloadOnChange: true);

// Configure Serilog
Log.Logger = new LoggerConfiguration()
    .ReadFrom.Configuration(builder.Configuration)
    .Enrich.FromLogContext()
    .WriteTo.Console()
    .WriteTo.File("logs/teamsbot-.log", rollingInterval: RollingInterval.Day)
    .CreateLogger();

builder.Host.UseSerilog();

// Load configuration sections
var botConfig = builder.Configuration.GetSection("Bot").Get<BotConfiguration>()
    ?? throw new InvalidOperationException("Bot configuration is missing");

var mediaConfig = builder.Configuration.GetSection("MediaPlatformSettings").Get<MediaPlatformConfiguration>()
    ?? throw new InvalidOperationException("MediaPlatformSettings configuration is missing");

var speechConfig = builder.Configuration.GetSection("Speech").Get<SpeechConfiguration>()
    ?? throw new InvalidOperationException("Speech configuration is missing");

var transcriptSinkConfig = builder.Configuration.GetSection("TranscriptSink").Get<TranscriptSinkConfiguration>()
    ?? throw new InvalidOperationException("TranscriptSink configuration is missing");

// Configure Kestrel to listen on the specified URL.
// If HTTPS is specified, bind the installed certificate by thumbprint.
var listenUri = new Uri(botConfig.LocalHttpListenUrl);
if (listenUri.Scheme.Equals("http", StringComparison.OrdinalIgnoreCase) && listenUri.Port == 443)
{
    throw new InvalidOperationException("LocalHttpListenUrl is HTTP on port 443. Use https:// for TLS or change the port.");
}
builder.WebHost.ConfigureKestrel(options =>
{
    options.ListenAnyIP(listenUri.Port, listenOptions =>
    {
        if (listenUri.Scheme.Equals("https", StringComparison.OrdinalIgnoreCase))
        {
            var cert = LoadCertificateFromStore(mediaConfig.CertificateThumbprint);
            listenOptions.UseHttps(cert);
        }
    });
});

// Register configuration as singletons
builder.Services.AddSingleton(botConfig);
builder.Services.AddSingleton(mediaConfig);
builder.Services.AddSingleton(speechConfig);
builder.Services.AddSingleton(transcriptSinkConfig);

// Register Graph logger (use SDK GraphLogger to satisfy IGraphLogger interface)
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

// Register Python transcript publisher as transient (each transcriber gets its own)
builder.Services.AddTransient(sp =>
{
    var logger = sp.GetRequiredService<ILogger<PythonTranscriptPublisher>>();
    return new PythonTranscriptPublisher(transcriptSinkConfig.PythonEndpoint, logger);
});

// Register Azure Speech transcriber as transient
// Lifetime is managed by CallHandler, not by DI container
builder.Services.AddTransient(sp =>
{
    var publisher = sp.GetRequiredService<PythonTranscriptPublisher>();
    var logger = sp.GetRequiredService<ILogger<AzureSpeechRealtimeTranscriber>>();
    return new AzureSpeechRealtimeTranscriber(
        speechConfig.Key,
        speechConfig.Region,
        speechConfig.RecognitionLanguage,
        publisher,
        logger);
});

// Register bot service as singleton
builder.Services.AddSingleton<TeamsCallingBotService>();

// Add controllers
builder.Services.AddControllers()
    .AddNewtonsoftJson(); // Graph SDK uses Newtonsoft.Json

// Add health checks
builder.Services.AddHealthChecks();

var app = builder.Build();

// Configure HTTP request pipeline
app.UseRouting();

app.MapControllers();
app.MapHealthChecks("/health");

// Initialize bot service on startup
var botService = app.Services.GetRequiredService<TeamsCallingBotService>();
await botService.InitializeAsync();

Log.Information("Teams Media Bot POC starting on {Url}", botConfig.LocalHttpListenUrl);
Log.Information("Notification URL configured: {NotificationUrl}", botConfig.NotificationUrl);
Log.Information("Media endpoint: {ServiceFqdn}:{PublicPort}", mediaConfig.ServiceFqdn, mediaConfig.InstancePublicPort);

await app.RunAsync();

// Cleanup
await botService.DisposeAsync();
Log.CloseAndFlush();

static X509Certificate2 LoadCertificateFromStore(string thumbprint)
{
    if (string.IsNullOrWhiteSpace(thumbprint) || thumbprint == "CHANGE_AFTER_CERT_INSTALL")
    {
        throw new InvalidOperationException("CertificateThumbprint is not set. Update Config/appsettings.json.");
    }

    var normalized = thumbprint.Replace(" ", string.Empty).ToUpperInvariant();
    using var store = new X509Store(StoreName.My, StoreLocation.LocalMachine);
    store.Open(OpenFlags.ReadOnly);

    var certs = store.Certificates.Find(
        X509FindType.FindByThumbprint,
        normalized,
        validOnly: false);

    if (certs.Count == 0)
    {
        throw new InvalidOperationException($"Certificate with thumbprint {normalized} not found in LocalMachine/My.");
    }

    return certs[0];
}
