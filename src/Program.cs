using Microsoft.Graph.Communications.Common.Telemetry;
using Serilog;
using TeamsMediaBot.Models;
using TeamsMediaBot.Services;

var builder = WebApplication.CreateBuilder(args);

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

// Register Graph logger
builder.Services.AddSingleton<IGraphLogger>(sp =>
{
    var logger = sp.GetRequiredService<ILogger<Program>>();
    return new GraphLogger(logger);
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

/// <summary>
/// Adapter to bridge ILogger to IGraphLogger
/// </summary>
internal class GraphLogger : IGraphLogger
{
    private readonly ILogger _logger;

    public GraphLogger(ILogger logger)
    {
        _logger = logger;
    }

    public void Error(string message, Exception? exception = null, [System.Runtime.CompilerServices.CallerMemberName] string? memberName = null, [System.Runtime.CompilerServices.CallerFilePath] string? filePath = null, [System.Runtime.CompilerServices.CallerLineNumber] int lineNumber = 0)
    {
        _logger.LogError(exception, "{Message} [{MemberName}@{FilePath}:{LineNumber}]", message, memberName, filePath, lineNumber);
    }

    public void Info(string message, [System.Runtime.CompilerServices.CallerMemberName] string? memberName = null, [System.Runtime.CompilerServices.CallerFilePath] string? filePath = null, [System.Runtime.CompilerServices.CallerLineNumber] int lineNumber = 0)
    {
        _logger.LogInformation("{Message} [{MemberName}]", message, memberName);
    }

    public void Verbose(string message, [System.Runtime.CompilerServices.CallerMemberName] string? memberName = null, [System.Runtime.CompilerServices.CallerFilePath] string? filePath = null, [System.Runtime.CompilerServices.CallerLineNumber] int lineNumber = 0)
    {
        _logger.LogDebug("{Message} [{MemberName}]", message, memberName);
    }

    public void Warn(string message, [System.Runtime.CompilerServices.CallerMemberName] string? memberName = null, [System.Runtime.CompilerServices.CallerFilePath] string? filePath = null, [System.Runtime.CompilerServices.CallerLineNumber] int lineNumber = 0)
    {
        _logger.LogWarning("{Message} [{MemberName}]", message, memberName);
    }
}
