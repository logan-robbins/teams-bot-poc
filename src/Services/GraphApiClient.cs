using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Identity.Client;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

public sealed class GraphApiClient
{
    private const string GraphBaseUrl = "https://graph.microsoft.com/v1.0/";
    private static readonly JsonSerializerOptions SerializerOptions = new(JsonSerializerDefaults.Web)
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly HttpClient _httpClient;
    private readonly ILogger<GraphApiClient> _logger;
    private readonly IConfidentialClientApplication _msalApp;

    public GraphApiClient(
        HttpClient httpClient,
        BotConfiguration botConfig,
        ILogger<GraphApiClient> logger)
    {
        _httpClient = httpClient ?? throw new ArgumentNullException(nameof(httpClient));
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));

        ArgumentNullException.ThrowIfNull(botConfig);
        _msalApp = ConfidentialClientApplicationBuilder
            .Create(botConfig.AppId)
            .WithClientSecret(botConfig.AppSecret)
            .WithAuthority($"https://login.microsoftonline.com/{botConfig.TenantId}")
            .Build();
    }

    public async Task<GraphSubscriptionRecord> EnsureSubscriptionAsync(
        GraphSubscriptionCreateRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);

        var existing = await FindSubscriptionByResourceAsync(request.Resource, cancellationToken);
        if (existing is not null)
        {
            if (existing.IsExpiredSoon())
            {
                existing = await RenewSubscriptionAsync(existing.Id, request.ExpirationDateTime, cancellationToken);
            }
            return existing;
        }

        using var response = await SendAsync(HttpMethod.Post, "subscriptions", request, cancellationToken);
        return await ReadSubscriptionAsync(response, cancellationToken);
    }

    public async Task<GraphSubscriptionRecord?> FindSubscriptionByResourceAsync(
        string resource,
        CancellationToken cancellationToken = default)
    {
        await foreach (var subscription in GetSubscriptionsAsync(cancellationToken))
        {
            if (string.Equals(subscription.Resource, resource, StringComparison.OrdinalIgnoreCase))
            {
                return subscription;
            }
        }

        return null;
    }

    public async IAsyncEnumerable<GraphSubscriptionRecord> GetSubscriptionsAsync(
        [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        string? nextUrl = "subscriptions";

        while (!string.IsNullOrWhiteSpace(nextUrl))
        {
            using var response = await SendAsync(HttpMethod.Get, nextUrl, body: null, cancellationToken);
            using var document = await JsonDocument.ParseAsync(
                await response.Content.ReadAsStreamAsync(cancellationToken),
                cancellationToken: cancellationToken);

            if (document.RootElement.TryGetProperty("value", out var items) && items.ValueKind == JsonValueKind.Array)
            {
                foreach (var item in items.EnumerateArray())
                {
                    var record = item.Deserialize<GraphSubscriptionRecord>(SerializerOptions);
                    if (record is not null)
                    {
                        yield return record;
                    }
                }
            }

            nextUrl = document.RootElement.TryGetProperty("@odata.nextLink", out var nextLink)
                ? nextLink.GetString()
                : null;
        }
    }

    public async Task<GraphSubscriptionRecord> RenewSubscriptionAsync(
        string subscriptionId,
        DateTimeOffset expirationDateTime,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(subscriptionId);

        using var response = await SendAsync(
            HttpMethod.Patch,
            $"subscriptions/{Uri.EscapeDataString(subscriptionId)}",
            new { expirationDateTime },
            cancellationToken);

        return await ReadSubscriptionAsync(response, cancellationToken);
    }

    public async Task DeleteSubscriptionAsync(string subscriptionId, CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(subscriptionId);

        var request = new HttpRequestMessage(
            HttpMethod.Delete,
            BuildUri($"subscriptions/{Uri.EscapeDataString(subscriptionId)}"));
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", await AcquireTokenAsync(cancellationToken));

        using var response = await _httpClient.SendAsync(request, cancellationToken);

        if (response.StatusCode is HttpStatusCode.NotFound)
        {
            return;
        }

        response.EnsureSuccessStatusCode();
    }

    public async Task<JsonDocument> GetResourceAsync(string resource, CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(resource);

        using var response = await SendAsync(HttpMethod.Get, resource, body: null, cancellationToken);
        var stream = await response.Content.ReadAsStreamAsync(cancellationToken);
        return await JsonDocument.ParseAsync(stream, cancellationToken: cancellationToken);
    }

    /// <summary>
    /// GETs a resource and returns the raw response body as text. Used
    /// for transcript content fetches (<c>text/vtt</c>) where we don't
    /// want JSON parsing.
    /// </summary>
    public async Task<string> GetResourceTextAsync(
        string resource,
        string? acceptContentType = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(resource);

        var request = new HttpRequestMessage(HttpMethod.Get, BuildUri(resource));
        request.Headers.Authorization = new AuthenticationHeaderValue(
            "Bearer", await AcquireTokenAsync(cancellationToken));
        if (!string.IsNullOrWhiteSpace(acceptContentType))
        {
            request.Headers.Accept.Clear();
            request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue(acceptContentType));
        }

        using var response = await _httpClient.SendAsync(request, cancellationToken);
        var body = await response.Content.ReadAsStringAsync(cancellationToken);
        if (!response.IsSuccessStatusCode)
        {
            _logger.LogWarning(
                "Graph GET {Url} returned {StatusCode}: {Body}",
                request.RequestUri, (int)response.StatusCode,
                body.Length > 400 ? body[..400] : body);
            throw new GraphApiException(response.StatusCode, body);
        }
        return body;
    }

    private async Task<HttpResponseMessage> SendAsync(
        HttpMethod method,
        string relativeOrAbsoluteUrl,
        object? body,
        CancellationToken cancellationToken)
    {
        var request = new HttpRequestMessage(method, BuildUri(relativeOrAbsoluteUrl));
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", await AcquireTokenAsync(cancellationToken));
        request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));

        if (body is not null)
        {
            request.Content = JsonContent.Create(body, options: SerializerOptions);
        }

        var response = await _httpClient.SendAsync(request, cancellationToken);
        if (response.IsSuccessStatusCode)
        {
            return response;
        }

        var errorBody = await response.Content.ReadAsStringAsync(cancellationToken);
        _logger.LogWarning(
            "Graph request {Method} {Url} failed with {StatusCode}: {Body}",
            method,
            request.RequestUri,
            (int)response.StatusCode,
            errorBody.Length > 400 ? errorBody[..400] : errorBody);

        throw new GraphApiException(response.StatusCode, errorBody);
    }

    private static Uri BuildUri(string relativeOrAbsoluteUrl)
    {
        if (Uri.TryCreate(relativeOrAbsoluteUrl, UriKind.Absolute, out var absolute))
        {
            return absolute;
        }

        return new Uri(new Uri(GraphBaseUrl), relativeOrAbsoluteUrl.TrimStart('/'));
    }

    private async Task<string> AcquireTokenAsync(CancellationToken cancellationToken)
    {
        var result = await _msalApp
            .AcquireTokenForClient(["https://graph.microsoft.com/.default"])
            .ExecuteAsync(cancellationToken);

        return result.AccessToken;
    }

    private static async Task<GraphSubscriptionRecord> ReadSubscriptionAsync(
        HttpResponseMessage response,
        CancellationToken cancellationToken)
    {
        var stream = await response.Content.ReadAsStreamAsync(cancellationToken);
        var record = await JsonSerializer.DeserializeAsync<GraphSubscriptionRecord>(
            stream,
            SerializerOptions,
            cancellationToken);

        return record ?? throw new InvalidOperationException("Graph returned an empty subscription payload.");
    }
}

public sealed class GraphApiException : Exception
{
    public GraphApiException(HttpStatusCode statusCode, string responseBody)
        : base($"Graph API request failed with {(int)statusCode}: {responseBody}")
    {
        StatusCode = statusCode;
        ResponseBody = responseBody;
    }

    public HttpStatusCode StatusCode { get; }
    public string ResponseBody { get; }
}

public sealed record GraphSubscriptionCreateRequest
{
    public required string ChangeType { get; init; }
    public required string NotificationUrl { get; init; }
    public required string LifecycleNotificationUrl { get; init; }
    public required string Resource { get; init; }
    public required DateTimeOffset ExpirationDateTime { get; init; }
    public string? ClientState { get; init; }
    public bool? IncludeResourceData { get; init; }
    public string? EncryptionCertificate { get; init; }
    public string? EncryptionCertificateId { get; init; }
}

public sealed record GraphSubscriptionRecord
{
    public required string Id { get; init; }
    public required string Resource { get; init; }
    public required string ChangeType { get; init; }
    public required DateTimeOffset ExpirationDateTime { get; init; }
    public string? ClientState { get; init; }

    public bool IsExpiredSoon(TimeSpan? leadTime = null)
    {
        var lead = leadTime ?? TimeSpan.FromMinutes(10);
        return ExpirationDateTime <= DateTimeOffset.UtcNow.Add(lead);
    }
}
