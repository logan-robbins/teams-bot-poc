namespace TeamsMediaBot.Models;

/// <summary>
/// Configuration model for bot settings
/// </summary>
public class BotConfiguration
{
    public required string TenantId { get; set; }
    public required string AppId { get; set; }
    public required string AppSecret { get; set; }
    public required string NotificationUrl { get; set; }
    public required string LocalHttpListenUrl { get; set; }
    public int LocalHttpListenPort { get; set; }
}

/// <summary>
/// Media platform settings for application-hosted media
/// Per Microsoft docs S2, S11, S14
/// </summary>
public class MediaPlatformConfiguration
{
    public required string ApplicationId { get; set; }
    public required string CertificateThumbprint { get; set; }
    public int InstanceInternalPort { get; set; }
    public int InstancePublicPort { get; set; }
    public required string ServiceFqdn { get; set; }
    public required string InstancePublicIPAddress { get; set; }
}

/// <summary>
/// Azure Speech Service configuration
/// </summary>
public class SpeechConfiguration
{
    public required string Key { get; set; }
    public required string Region { get; set; }
    public required string RecognitionLanguage { get; set; }
}

/// <summary>
/// STT (speech-to-text) provider selection.
/// This enables choosing a provider/model without changing the call pipeline.
/// </summary>
public class SttConfiguration
{
    /// <summary>
    /// Provider name (e.g. "AzureSpeech").
    /// </summary>
    public required string Provider { get; set; }

    /// <summary>
    /// Azure Speech provider settings (used when Provider == "AzureSpeech").
    /// </summary>
    public AzureSpeechProviderConfiguration? AzureSpeech { get; set; }
}

public class AzureSpeechProviderConfiguration
{
    public required string Key { get; set; }
    public required string Region { get; set; }
    public required string RecognitionLanguage { get; set; }

    /// <summary>
    /// Optional Custom Speech model endpoint ID (model selection).
    /// If set, Speech SDK will target this custom model.
    /// </summary>
    public string? EndpointId { get; set; }
}

/// <summary>
/// Python transcript sink configuration
/// </summary>
public class TranscriptSinkConfiguration
{
    public required string PythonEndpoint { get; set; }
}
