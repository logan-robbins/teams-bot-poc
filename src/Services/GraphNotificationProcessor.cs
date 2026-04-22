using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using Microsoft.AspNetCore.WebUtilities;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

public sealed partial class GraphNotificationProcessor
{
    private static readonly JsonSerializerOptions SerializerOptions = new(JsonSerializerDefaults.Web)
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly PythonChatPublisher _chatPublisher;
    private readonly MeetingChatConfiguration _config;
    private readonly BotConfiguration _botConfig;
    private readonly IMeetingChatService _meetingChatService;
    private readonly GraphApiClient _graphApiClient;
    private readonly GraphNotificationCrypto _crypto;
    private readonly GraphValidationTokenValidator _tokenValidator;
    private readonly ILogger<GraphNotificationProcessor> _logger;

    public GraphNotificationProcessor(
        PythonChatPublisher chatPublisher,
        MeetingChatConfiguration config,
        BotConfiguration botConfig,
        IMeetingChatService meetingChatService,
        GraphApiClient graphApiClient,
        GraphNotificationCrypto crypto,
        GraphValidationTokenValidator tokenValidator,
        ILogger<GraphNotificationProcessor> logger)
    {
        _chatPublisher = chatPublisher;
        _config = config;
        _botConfig = botConfig;
        _meetingChatService = meetingChatService;
        _graphApiClient = graphApiClient;
        _crypto = crypto;
        _tokenValidator = tokenValidator;
        _logger = logger;
    }

    public async Task ProcessAsync(string requestBody, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(requestBody))
        {
            return;
        }

        GraphNotificationEnvelope? envelope;
        try
        {
            envelope = JsonSerializer.Deserialize<GraphNotificationEnvelope>(requestBody, SerializerOptions);
        }
        catch (JsonException ex)
        {
            _logger.LogWarning(ex, "Failed to deserialize Graph notification payload.");
            return;
        }

        if (envelope is null || envelope.Value.Count == 0)
        {
            return;
        }

        if (!await _tokenValidator.ValidateAsync(envelope.ValidationTokens, cancellationToken))
        {
            _logger.LogWarning("Dropping Graph notification batch because validationTokens failed verification.");
            return;
        }

        foreach (var notification in envelope.Value)
        {
            try
            {
                if (!ValidateClientState(notification.ClientState))
                {
                    continue;
                }

                if (!string.IsNullOrWhiteSpace(notification.LifecycleEvent))
                {
                    await _meetingChatService.HandleLifecycleEventAsync(
                        notification.SubscriptionId,
                        notification.LifecycleEvent,
                        cancellationToken);
                    continue;
                }

                await ProcessMessageNotificationAsync(notification, cancellationToken);
            }
            catch (Exception ex)
            {
                _logger.LogError(
                    ex,
                    "Failed to process Graph notification subscription={SubscriptionId} resource={Resource}",
                    notification.SubscriptionId,
                    notification.Resource);
            }
        }
    }

    private async Task ProcessMessageNotificationAsync(
        GraphNotification notification,
        CancellationToken cancellationToken)
    {
        JsonDocument? document = null;

        try
        {
            document = await ResolveMessagePayloadAsync(notification, cancellationToken);
            var payload = BuildChatEventPayload(notification, document);
            if (payload is null)
            {
                return;
            }

            if (!_meetingChatService.IsTrackedChatThread(payload.ChatThreadId))
            {
                _logger.LogDebug("Skipping Graph chat event for inactive thread {ChatThreadId}", payload.ChatThreadId);
                return;
            }

            await _chatPublisher.PublishAsync(payload, cancellationToken);
        }
        finally
        {
            document?.Dispose();
        }
    }

    private async Task<JsonDocument?> ResolveMessagePayloadAsync(
        GraphNotification notification,
        CancellationToken cancellationToken)
    {
        if (notification.EncryptedContent is not null)
        {
            return _crypto.DecryptPayload(notification.EncryptedContent);
        }

        if (string.Equals(notification.ChangeType, "deleted", StringComparison.OrdinalIgnoreCase))
        {
            return null;
        }

        var resource = notification.ResourceData?.OdataId ?? notification.Resource;
        if (string.IsNullOrWhiteSpace(resource))
        {
            return null;
        }

        return await _graphApiClient.GetResourceAsync(resource, cancellationToken);
    }

    private ChatEventPayload? BuildChatEventPayload(GraphNotification notification, JsonDocument? document)
    {
        var root = document?.RootElement;
        var chatThreadId = TryGetString(root, "chatId")
            ?? ParseChatThreadId(notification.ResourceData?.OdataId)
            ?? ParseChatThreadId(notification.Resource);

        if (string.IsNullOrWhiteSpace(chatThreadId))
        {
            _logger.LogDebug("Graph notification did not include a chat thread id.");
            return null;
        }

        var messageId = TryGetString(root, "id")
            ?? notification.ResourceData?.Id
            ?? ParseMessageId(notification.ResourceData?.OdataId)
            ?? ParseMessageId(notification.Resource)
            ?? Guid.NewGuid().ToString("N");

        var timestamp = TryGetString(root, "lastModifiedDateTime")
            ?? TryGetString(root, "createdDateTime")
            ?? DateTimeOffset.UtcNow.UtcDateTime.ToString("o");

        var html = TryGetNestedString(root, "body", "content");
        var text = html is null ? TryGetNestedString(root, "body", "content") : StripHtml(html);
        if (string.IsNullOrWhiteSpace(text))
        {
            text = TryGetString(root, "summary");
        }

        var senderId = TryGetNestedString(root, "from", "user", "id")
            ?? TryGetNestedString(root, "from", "application", "id");
        var senderDisplayName = TryGetNestedString(root, "from", "user", "displayName")
            ?? TryGetNestedString(root, "from", "application", "displayName");
        var senderApplicationId = TryGetNestedString(root, "from", "application", "id");

        return new ChatEventPayload
        {
            EventType = MapEventType(notification.ChangeType),
            ChatThreadId = chatThreadId,
            MessageId = messageId,
            Text = text,
            Html = html,
            SenderId = senderId,
            SenderDisplayName = senderDisplayName,
            TimestampUtc = timestamp,
            ConversationReferenceId = chatThreadId,
            Attachments = DeserializeJsonList(root, "attachments"),
            Mentions = DeserializeJsonList(root, "mentions"),
            ReplyToMessageId = TryGetString(root, "replyToId"),
            FromBot = string.Equals(senderApplicationId, _botConfig.AppId, StringComparison.OrdinalIgnoreCase)
                || string.Equals(senderId, _botConfig.AppId, StringComparison.OrdinalIgnoreCase),
            Raw = root.HasValue
                ? JsonSerializer.Deserialize<Dictionary<string, object?>>(root.Value.GetRawText(), SerializerOptions)
                : BuildMinimalRaw(notification, chatThreadId, messageId),
        };
    }

    private bool ValidateClientState(string? clientState)
    {
        if (string.IsNullOrWhiteSpace(_config.ChatSubscriptionClientStateSecret))
        {
            return true;
        }

        if (!string.Equals(clientState, _config.ChatSubscriptionClientStateSecret, StringComparison.Ordinal))
        {
            _logger.LogWarning("Dropping Graph notification with invalid clientState.");
            return false;
        }

        return true;
    }

    private static string MapEventType(string? changeType) =>
        changeType?.ToLowerInvariant() switch
        {
            "updated" => "chat_updated",
            "deleted" => "chat_deleted",
            _ => "chat_created",
        };

    private static Dictionary<string, object?> BuildMinimalRaw(
        GraphNotification notification,
        string chatThreadId,
        string messageId) =>
        new()
        {
            ["resource"] = notification.Resource,
            ["change_type"] = notification.ChangeType,
            ["chat_id"] = chatThreadId,
            ["message_id"] = messageId,
        };

    private static List<Dictionary<string, object?>> DeserializeJsonList(JsonElement? root, string propertyName)
    {
        if (!root.HasValue || !root.Value.TryGetProperty(propertyName, out var property) || property.ValueKind != JsonValueKind.Array)
        {
            return [];
        }

        var result = new List<Dictionary<string, object?>>();
        foreach (var item in property.EnumerateArray())
        {
            var parsed = JsonSerializer.Deserialize<Dictionary<string, object?>>(item.GetRawText(), SerializerOptions);
            if (parsed is not null)
            {
                result.Add(parsed);
            }
        }

        return result;
    }

    private static string? TryGetString(JsonElement? root, string propertyName)
    {
        if (!root.HasValue || !root.Value.TryGetProperty(propertyName, out var property))
        {
            return null;
        }

        return property.ValueKind == JsonValueKind.String ? property.GetString() : property.GetRawText();
    }

    private static string? TryGetNestedString(JsonElement? root, params string[] path)
    {
        if (!root.HasValue)
        {
            return null;
        }

        var current = root.Value;
        foreach (var segment in path)
        {
            if (!current.TryGetProperty(segment, out var next))
            {
                return null;
            }

            current = next;
        }

        return current.ValueKind == JsonValueKind.String ? current.GetString() : current.GetRawText();
    }

    private static string? ParseChatThreadId(string? resource)
    {
        if (string.IsNullOrWhiteSpace(resource))
        {
            return null;
        }

        var path = ExtractPath(resource);
        var segments = path.Split('/', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        for (var index = 0; index < segments.Length - 1; index++)
        {
            if (string.Equals(segments[index], "chats", StringComparison.OrdinalIgnoreCase))
            {
                return Uri.UnescapeDataString(segments[index + 1]);
            }
        }

        return null;
    }

    private static string? ParseMessageId(string? resource)
    {
        if (string.IsNullOrWhiteSpace(resource))
        {
            return null;
        }

        var path = ExtractPath(resource);
        var segments = path.Split('/', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        for (var index = 0; index < segments.Length - 1; index++)
        {
            if (string.Equals(segments[index], "messages", StringComparison.OrdinalIgnoreCase))
            {
                return Uri.UnescapeDataString(segments[index + 1]);
            }
        }

        return null;
    }

    private static string ExtractPath(string resource)
    {
        if (Uri.TryCreate(resource, UriKind.Absolute, out var absolute))
        {
            return absolute.AbsolutePath;
        }

        return QueryHelpers.ParseQuery(resource).Count > 0
            ? resource.Split('?', 2)[0]
            : resource;
    }

    private static string? StripHtml(string? html)
    {
        if (string.IsNullOrWhiteSpace(html))
        {
            return html;
        }

        return CollapseWhitespaceRegex().Replace(HtmlTagRegex().Replace(System.Net.WebUtility.HtmlDecode(html), " "), " ").Trim();
    }

    [GeneratedRegex("<[^>]+>", RegexOptions.Compiled)]
    private static partial Regex HtmlTagRegex();

    [GeneratedRegex("\\s+", RegexOptions.Compiled)]
    private static partial Regex CollapseWhitespaceRegex();
}

public sealed record GraphNotificationEnvelope
{
    [JsonPropertyName("value")]
    public List<GraphNotification> Value { get; init; } = [];

    [JsonPropertyName("validationTokens")]
    public List<string>? ValidationTokens { get; init; }
}

public sealed record GraphNotification
{
    [JsonPropertyName("subscriptionId")]
    public string? SubscriptionId { get; init; }

    [JsonPropertyName("changeType")]
    public string? ChangeType { get; init; }

    [JsonPropertyName("resource")]
    public string? Resource { get; init; }

    [JsonPropertyName("clientState")]
    public string? ClientState { get; init; }

    [JsonPropertyName("tenantId")]
    public string? TenantId { get; init; }

    [JsonPropertyName("subscriptionExpirationDateTime")]
    public DateTimeOffset? SubscriptionExpirationDateTime { get; init; }

    [JsonPropertyName("lifecycleEvent")]
    public string? LifecycleEvent { get; init; }

    [JsonPropertyName("resourceData")]
    public GraphResourceData? ResourceData { get; init; }

    [JsonPropertyName("encryptedContent")]
    public GraphEncryptedContent? EncryptedContent { get; init; }
}

public sealed record GraphResourceData
{
    [JsonPropertyName("@odata.type")]
    public string? OdataType { get; init; }

    [JsonPropertyName("@odata.id")]
    public string? OdataId { get; init; }

    [JsonPropertyName("id")]
    public string? Id { get; init; }
}

public sealed record GraphEncryptedContent
{
    [JsonPropertyName("data")]
    public required string Data { get; init; }

    [JsonPropertyName("dataSignature")]
    public required string DataSignature { get; init; }

    [JsonPropertyName("dataKey")]
    public required string DataKey { get; init; }

    [JsonPropertyName("encryptionCertificateId")]
    public string? EncryptionCertificateId { get; init; }

    [JsonPropertyName("encryptionCertificateThumbprint")]
    public string? EncryptionCertificateThumbprint { get; init; }
}
