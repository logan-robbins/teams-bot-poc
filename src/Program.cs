using Microsoft.Graph.Communications.Common.Telemetry;
using Microsoft.Extensions.Logging;
using Serilog;
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

// Register Python transcript publisher
builder.Services.AddScoped(sp =>
{
    var logger = sp.GetRequiredService<ILogger<PythonTranscriptPublisher>>();
    return new PythonTranscriptPublisher(transcriptSinkConfig.PythonEndpoint, logger);
});

// Register Azure Speech transcriber
builder.Services.AddScoped(sp =>
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

// Configure Kestrel to listen on the specified port per S2
app.Urls.Clear();
app.Urls.Add(botConfig.LocalHttpListenUrl);

await app.RunAsync();

// Cleanup
await botService.DisposeAsync();
Log.CloseAndFlush();
