namespace TeamsMediaBot.Models;

/// <summary>
/// Configuration model for Azure AD bot registration settings.
/// </summary>
public sealed class BotConfiguration
{
    /// <summary>
    /// Gets or sets the Azure AD tenant ID.
    /// </summary>
    public required string TenantId { get; init; }
    
    /// <summary>
    /// Gets or sets the Azure AD application (client) ID.
    /// </summary>
    public required string AppId { get; init; }
    
    /// <summary>
    /// Gets or sets the Azure AD application client secret.
    /// </summary>
    public required string AppSecret { get; init; }
    
    /// <summary>
    /// Gets or sets the public URL where Microsoft Graph sends webhook notifications.
    /// </summary>
    /// <example>https://teamsbot.example.com/api/calling</example>
    public required string NotificationUrl { get; init; }
    
    /// <summary>
    /// Gets or sets the local URL where Kestrel listens for HTTP requests.
    /// </summary>
    /// <example>https://0.0.0.0:9443</example>
    public required string LocalHttpListenUrl { get; init; }
    
    /// <summary>
    /// Gets or sets the local HTTP listen port (deprecated, use LocalHttpListenUrl).
    /// </summary>
    [Obsolete("Use LocalHttpListenUrl instead. This property is kept for backward compatibility.")]
    public int LocalHttpListenPort { get; init; }
}

/// <summary>
/// Media platform settings for application-hosted media.
/// </summary>
/// <remarks>
/// Per Microsoft Graph Communications SDK documentation, these settings are required
/// for bots that handle audio/video streams directly.
/// </remarks>
public sealed class MediaPlatformConfiguration
{
    /// <summary>
    /// Gets or sets the Azure AD application ID (same as bot AppId).
    /// </summary>
    public required string ApplicationId { get; init; }
    
    /// <summary>
    /// Gets or sets the thumbprint of the TLS certificate in the LocalMachine store.
    /// </summary>
    public required string CertificateThumbprint { get; init; }
    
    /// <summary>
    /// Gets or sets the internal port for media traffic (typically 8445).
    /// </summary>
    public int InstanceInternalPort { get; init; }
    
    /// <summary>
    /// Gets or sets the public port for media traffic (typically same as internal).
    /// </summary>
    public int InstancePublicPort { get; init; }
    
    /// <summary>
    /// Gets or sets the fully qualified domain name for the media endpoint.
    /// </summary>
    /// <example>media.example.com</example>
    public required string ServiceFqdn { get; init; }
    
    /// <summary>
    /// Gets or sets the public IP address (or "0.0.0.0" to listen on all interfaces).
    /// </summary>
    public required string InstancePublicIPAddress { get; init; }
}

/// <summary>
/// Legacy Azure Speech Service configuration (for backward compatibility).
/// </summary>
/// <remarks>
/// Use <see cref="SttConfiguration"/> with <see cref="AzureSpeechProviderConfiguration"/> instead.
/// </remarks>
public sealed class SpeechConfiguration
{
    /// <summary>
    /// Gets or sets the Azure Speech subscription key.
    /// </summary>
    public required string Key { get; init; }
    
    /// <summary>
    /// Gets or sets the Azure Speech region (e.g., "eastus").
    /// </summary>
    public required string Region { get; init; }
    
    /// <summary>
    /// Gets or sets the recognition language (e.g., "en-US").
    /// </summary>
    public required string RecognitionLanguage { get; init; }
}

/// <summary>
/// STT (speech-to-text) provider configuration.
/// </summary>
/// <remarks>
/// Enables choosing a provider/model without changing the call pipeline.
/// Supported providers: "Deepgram" (recommended) and "AzureSpeech" (fallback).
/// </remarks>
public sealed class SttConfiguration
{
    /// <summary>
    /// Gets or sets the STT provider name ("Deepgram" or "AzureSpeech").
    /// </summary>
    /// <remarks>
    /// Default is "Deepgram" which provides best diarization quality.
    /// </remarks>
    public string Provider { get; set; } = "Deepgram";
    
    /// <summary>
    /// Gets or sets the Deepgram-specific configuration.
    /// </summary>
    public DeepgramConfiguration? Deepgram { get; set; }
    
    /// <summary>
    /// Gets or sets the Azure Speech-specific configuration.
    /// </summary>
    public AzureSpeechProviderConfiguration? AzureSpeech { get; set; }
}

/// <summary>
/// Deepgram STT provider configuration.
/// </summary>
public sealed class DeepgramConfiguration
{
    /// <summary>
    /// Gets or sets the Deepgram API key.
    /// </summary>
    public required string ApiKey { get; init; }
    
    /// <summary>
    /// Gets or sets the Deepgram model to use.
    /// </summary>
    /// <remarks>
    /// Recommended: "nova-3" for 2025/2026.
    /// </remarks>
    public string Model { get; init; } = "nova-3";
    
    /// <summary>
    /// Gets or sets whether to enable speaker diarization.
    /// </summary>
    /// <remarks>
    /// MUST be true for meeting transcription to identify speakers.
    /// </remarks>
    public bool Diarize { get; init; } = true;
}

/// <summary>
/// Azure Speech STT provider configuration.
/// </summary>
public sealed class AzureSpeechProviderConfiguration
{
    /// <summary>
    /// Gets or sets the Azure Speech subscription key.
    /// </summary>
    public required string Key { get; init; }
    
    /// <summary>
    /// Gets or sets the Azure Speech region (e.g., "eastus").
    /// </summary>
    public required string Region { get; init; }
    
    /// <summary>
    /// Gets or sets the recognition language.
    /// </summary>
    /// <remarks>
    /// Default is "en-US". Other supported languages depend on Azure Speech service.
    /// </remarks>
    public string RecognitionLanguage { get; init; } = "en-US";
    
    /// <summary>
    /// Gets or sets the optional Custom Speech endpoint ID.
    /// </summary>
    /// <remarks>
    /// Leave null to use the standard Azure Speech endpoint.
    /// </remarks>
    public string? EndpointId { get; init; }
}

/// <summary>
/// Alias for <see cref="AzureSpeechProviderConfiguration"/> for configuration binding.
/// </summary>
public sealed class AzureSpeechConfiguration
{
    /// <inheritdoc cref="AzureSpeechProviderConfiguration.Key"/>
    public required string Key { get; init; }
    
    /// <inheritdoc cref="AzureSpeechProviderConfiguration.Region"/>
    public required string Region { get; init; }
    
    /// <inheritdoc cref="AzureSpeechProviderConfiguration.RecognitionLanguage"/>
    public string RecognitionLanguage { get; init; } = "en-US";
    
    /// <inheritdoc cref="AzureSpeechProviderConfiguration.EndpointId"/>
    public string? EndpointId { get; init; }
}

/// <summary>
/// Python transcript sink configuration.
/// </summary>
public sealed class TranscriptSinkConfiguration
{
    /// <summary>
    /// Gets or sets the Python FastAPI endpoint URL for receiving transcript events.
    /// </summary>
    /// <example>https://agent.example.com/transcript</example>
    public required string PythonEndpoint { get; init; }
}
