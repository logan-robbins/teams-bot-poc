using Microsoft.Graph.Communications.Calls;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

public interface IMeetingChatService
{
    Task AttachToCallAsync(ICall call, CancellationToken cancellationToken = default);
    Task DetachFromCallAsync(ICall call, CancellationToken cancellationToken = default);
    string? GetChatThreadIdForCall(string callId);
    bool IsTrackedChatThread(string chatThreadId);
    bool IsTrackedChannel(string teamId, string channelId);

    /// <summary>
    /// Returns true when the bot is actively listening on the given thread,
    /// regardless of whether the thread belongs to a meeting chat or a team
    /// channel. Used by <see cref="GraphNotificationProcessor"/> to filter.
    /// </summary>
    bool IsTrackedConversationThread(string threadId);

    Task<ChannelSubscriptionResult> EnsureChannelMessagesSubscriptionAsync(
        string teamId,
        string channelId,
        CancellationToken cancellationToken = default);

    Task DeleteChannelMessagesSubscriptionAsync(
        string teamId,
        string channelId,
        CancellationToken cancellationToken = default);

    Task HandleLifecycleEventAsync(
        string? subscriptionId,
        string? lifecycleEvent,
        CancellationToken cancellationToken = default);
}

/// <summary>
/// Result of attaching a Graph subscription to a channel's messages
/// resource. Returned to <see cref="ChannelAttachmentService"/> so the
/// persistent attachment record can capture the subscription state.
/// </summary>
public sealed record ChannelSubscriptionResult
{
    public required string SubscriptionId { get; init; }
    public required string Resource { get; init; }
    public required DateTimeOffset ExpiresAtUtc { get; init; }
}

public sealed class MeetingChatService : IMeetingChatService, IAsyncDisposable
{
    private static readonly TimeSpan SubscriptionLength = TimeSpan.FromMinutes(55);
    private static readonly TimeSpan RenewalLeadTime = TimeSpan.FromMinutes(10);
    private static readonly TimeSpan RenewalCheckInterval = TimeSpan.FromMinutes(5);

    private readonly MeetingChatConfiguration _config;
    private readonly GraphApiClient _graphApiClient;
    private readonly GraphNotificationCrypto _crypto;
    private readonly ILogger<MeetingChatService> _logger;
    private readonly SemaphoreSlim _mutex = new(1, 1);
    private readonly CancellationTokenSource _renewalLoopCts = new();

    private readonly Dictionary<string, string> _callToChatThread = new(StringComparer.Ordinal);
    private readonly Dictionary<string, int> _activeChatThreadRefCounts = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _resourceToSubscriptionId = new(StringComparer.Ordinal);
    private readonly Dictionary<string, GraphSubscriptionRecord> _subscriptionsById = new(StringComparer.Ordinal);

    private readonly HashSet<string> _attachedChannelKeys = new(StringComparer.Ordinal);

    private Task? _renewalLoopTask;
    private bool _disposed;

    public MeetingChatService(
        MeetingChatConfiguration config,
        GraphApiClient graphApiClient,
        GraphNotificationCrypto crypto,
        ILogger<MeetingChatService> logger)
    {
        _config = config;
        _graphApiClient = graphApiClient;
        _crypto = crypto;
        _logger = logger;
    }

    public async Task AttachToCallAsync(ICall call, CancellationToken cancellationToken = default)
    {
        if (!_config.Enabled)
        {
            return;
        }

        var chatThreadId = ExtractChatThreadId(call);
        if (string.IsNullOrWhiteSpace(chatThreadId))
        {
            _logger.LogWarning("Call {CallId} has no chatInfo.threadId; skipping Graph chat tracking.", call.Id);
            return;
        }

        await _mutex.WaitAsync(cancellationToken);
        try
        {
            _callToChatThread[call.Id] = chatThreadId;
            _activeChatThreadRefCounts[chatThreadId] = _activeChatThreadRefCounts.GetValueOrDefault(chatThreadId) + 1;
            EnsureRenewalLoopStarted();

            if (!CanManageGraphSubscriptions())
            {
                _logger.LogInformation(
                    "Meeting chat tracking enabled for thread {ChatThreadId}, but Graph subscription configuration is incomplete.",
                    chatThreadId);
                return;
            }

            var resource = ResolveSubscriptionResource(chatThreadId);
            var subscription = await EnsureSubscriptionLockedAsync(resource, cancellationToken);

            _logger.LogInformation(
                "Tracking meeting chat thread {ChatThreadId} with subscription {SubscriptionId}",
                chatThreadId,
                subscription.Id);
        }
        finally
        {
            _mutex.Release();
        }
    }

    public async Task DetachFromCallAsync(ICall call, CancellationToken cancellationToken = default)
    {
        var chatThreadId = ExtractChatThreadId(call);
        if (string.IsNullOrWhiteSpace(chatThreadId))
        {
            return;
        }

        string? resourceToDelete = null;
        string? subscriptionIdToDelete = null;

        await _mutex.WaitAsync(cancellationToken);
        try
        {
            _callToChatThread.Remove(call.Id);

            if (_activeChatThreadRefCounts.TryGetValue(chatThreadId, out var currentCount))
            {
                if (currentCount <= 1)
                {
                    _activeChatThreadRefCounts.Remove(chatThreadId);
                }
                else
                {
                    _activeChatThreadRefCounts[chatThreadId] = currentCount - 1;
                }
            }

            if (ShouldUseSharedInstalledChatsSubscription() || _activeChatThreadRefCounts.ContainsKey(chatThreadId))
            {
                return;
            }

            resourceToDelete = BuildPerChatMessagesResource(chatThreadId);
            if (_resourceToSubscriptionId.TryGetValue(resourceToDelete, out var subscriptionId))
            {
                subscriptionIdToDelete = subscriptionId;
                UnregisterSubscriptionLocked(subscriptionId);
            }
        }
        finally
        {
            _mutex.Release();
        }

        if (!string.IsNullOrWhiteSpace(subscriptionIdToDelete))
        {
            try
            {
                await _graphApiClient.DeleteSubscriptionAsync(subscriptionIdToDelete, cancellationToken);
                _logger.LogInformation(
                    "Deleted per-chat Graph subscription {SubscriptionId} for resource {Resource}",
                    subscriptionIdToDelete,
                    resourceToDelete);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(
                    ex,
                    "Failed to delete Graph subscription {SubscriptionId} for resource {Resource}",
                    subscriptionIdToDelete,
                    resourceToDelete);
            }
        }
    }

    public string? GetChatThreadIdForCall(string callId) =>
        _callToChatThread.TryGetValue(callId, out var threadId) ? threadId : null;

    public bool IsTrackedChatThread(string chatThreadId)
    {
        if (string.IsNullOrWhiteSpace(chatThreadId))
        {
            return false;
        }

        return _activeChatThreadRefCounts.ContainsKey(chatThreadId);
    }

    public bool IsTrackedChannel(string teamId, string channelId)
    {
        if (string.IsNullOrWhiteSpace(teamId) || string.IsNullOrWhiteSpace(channelId))
        {
            return false;
        }

        var resource = BuildChannelMessagesResource(teamId, channelId);
        return _resourceToSubscriptionId.ContainsKey(resource)
            || _attachedChannelKeys.Contains(BuildChannelKey(teamId, channelId));
    }

    public bool IsTrackedConversationThread(string threadId)
    {
        if (string.IsNullOrWhiteSpace(threadId))
        {
            return false;
        }

        if (_activeChatThreadRefCounts.ContainsKey(threadId))
        {
            return true;
        }

        // Channel conversation ids look like "19:{channelId}@thread.tacv2"; we
        // don't have a reverse map from conversation id to (teamId, channelId)
        // here, so the GraphNotificationProcessor must compare against the
        // resource path directly. Returning false is correct: channel
        // notifications are gated on resource (teams/.../channels/.../messages),
        // not on conversation thread id.
        return false;
    }

    public async Task<ChannelSubscriptionResult> EnsureChannelMessagesSubscriptionAsync(
        string teamId,
        string channelId,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(teamId);
        ArgumentException.ThrowIfNullOrWhiteSpace(channelId);

        if (!CanManageGraphSubscriptions())
        {
            throw new InvalidOperationException(
                "Cannot manage channel subscriptions: MeetingChat.GraphNotificationBaseUrl and "
                + "ChatSubscriptionClientStateSecret must be configured.");
        }

        var resource = BuildChannelMessagesResource(teamId, channelId);

        await _mutex.WaitAsync(cancellationToken);
        try
        {
            EnsureRenewalLoopStarted();
            _attachedChannelKeys.Add(BuildChannelKey(teamId, channelId));

            var subscription = await EnsureSubscriptionLockedAsync(resource, cancellationToken);
            _logger.LogInformation(
                "Ensured channel-messages subscription {SubscriptionId} for resource {Resource}",
                subscription.Id,
                subscription.Resource);

            return new ChannelSubscriptionResult
            {
                SubscriptionId = subscription.Id,
                Resource = subscription.Resource,
                ExpiresAtUtc = subscription.ExpirationDateTime,
            };
        }
        finally
        {
            _mutex.Release();
        }
    }

    public async Task DeleteChannelMessagesSubscriptionAsync(
        string teamId,
        string channelId,
        CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(teamId) || string.IsNullOrWhiteSpace(channelId))
        {
            return;
        }

        string? subscriptionIdToDelete = null;
        var resource = BuildChannelMessagesResource(teamId, channelId);

        await _mutex.WaitAsync(cancellationToken);
        try
        {
            _attachedChannelKeys.Remove(BuildChannelKey(teamId, channelId));

            if (_resourceToSubscriptionId.TryGetValue(resource, out var subscriptionId))
            {
                subscriptionIdToDelete = subscriptionId;
                UnregisterSubscriptionLocked(subscriptionId);
            }
        }
        finally
        {
            _mutex.Release();
        }

        if (!string.IsNullOrWhiteSpace(subscriptionIdToDelete))
        {
            try
            {
                await _graphApiClient.DeleteSubscriptionAsync(subscriptionIdToDelete, cancellationToken);
                _logger.LogInformation(
                    "Deleted channel subscription {SubscriptionId} for resource {Resource}",
                    subscriptionIdToDelete,
                    resource);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(
                    ex,
                    "Failed to delete channel subscription {SubscriptionId} for resource {Resource}",
                    subscriptionIdToDelete,
                    resource);
            }
        }
    }

    private static string BuildChannelMessagesResource(string teamId, string channelId) =>
        $"teams/{Uri.EscapeDataString(teamId)}/channels/{Uri.EscapeDataString(channelId)}/messages";

    private static string BuildChannelKey(string teamId, string channelId) =>
        $"{teamId}|{channelId}";

    public async Task HandleLifecycleEventAsync(
        string? subscriptionId,
        string? lifecycleEvent,
        CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(lifecycleEvent))
        {
            return;
        }

        GraphSubscriptionRecord? knownSubscription = null;
        string? recreateResource = null;

        await _mutex.WaitAsync(cancellationToken);
        try
        {
            if (!string.IsNullOrWhiteSpace(subscriptionId)
                && _subscriptionsById.TryGetValue(subscriptionId, out var subscription))
            {
                knownSubscription = subscription;
            }

            switch (lifecycleEvent.Trim())
            {
                case "reauthorizationRequired":
                    break;

                case "subscriptionRemoved":
                case "missed":
                    recreateResource = knownSubscription?.Resource;
                    if (!string.IsNullOrWhiteSpace(subscriptionId))
                    {
                        UnregisterSubscriptionLocked(subscriptionId);
                    }
                    break;
            }

            if (string.IsNullOrWhiteSpace(recreateResource) && ShouldUseSharedInstalledChatsSubscription())
            {
                recreateResource = BuildInstalledChatsResource();
            }
        }
        finally
        {
            _mutex.Release();
        }

        if (knownSubscription is not null
            && string.Equals(lifecycleEvent, "reauthorizationRequired", StringComparison.OrdinalIgnoreCase))
        {
            var renewed = await _graphApiClient.RenewSubscriptionAsync(
                knownSubscription.Id,
                DateTimeOffset.UtcNow.Add(SubscriptionLength),
                cancellationToken);
            await RememberSubscriptionAsync(renewed, cancellationToken);
            return;
        }

        if (!string.IsNullOrWhiteSpace(recreateResource))
        {
            await EnsureSubscriptionAsync(recreateResource, cancellationToken);
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        _renewalLoopCts.Cancel();

        if (_renewalLoopTask is not null)
        {
            try
            {
                await _renewalLoopTask;
            }
            catch (OperationCanceledException)
            {
                // normal shutdown
            }
        }

        _renewalLoopCts.Dispose();
        _mutex.Dispose();
    }

    private bool CanManageGraphSubscriptions() =>
        !string.IsNullOrWhiteSpace(_config.GraphNotificationBaseUrl)
        && !string.IsNullOrWhiteSpace(_config.ChatSubscriptionClientStateSecret);

    private bool ShouldUseSharedInstalledChatsSubscription() =>
        _config.UseInstalledToChatsSubscription
        && !string.IsNullOrWhiteSpace(_config.TeamsAppCatalogId);

    private string ResolveSubscriptionResource(string chatThreadId)
    {
        if (ShouldUseSharedInstalledChatsSubscription())
        {
            return BuildInstalledChatsResource();
        }

        return BuildPerChatMessagesResource(chatThreadId);
    }

    private async Task<GraphSubscriptionRecord> EnsureSubscriptionAsync(
        string resource,
        CancellationToken cancellationToken)
    {
        await _mutex.WaitAsync(cancellationToken);
        try
        {
            return await EnsureSubscriptionLockedAsync(resource, cancellationToken);
        }
        finally
        {
            _mutex.Release();
        }
    }

    private async Task<GraphSubscriptionRecord> EnsureSubscriptionLockedAsync(
        string resource,
        CancellationToken cancellationToken)
    {
        if (_resourceToSubscriptionId.TryGetValue(resource, out var knownId)
            && _subscriptionsById.TryGetValue(knownId, out var knownSubscription)
            && !knownSubscription.IsExpiredSoon(RenewalLeadTime))
        {
            return knownSubscription;
        }

        var request = new GraphSubscriptionCreateRequest
        {
            ChangeType = "created,updated,deleted",
            NotificationUrl = BuildNotificationUrl(),
            LifecycleNotificationUrl = BuildNotificationUrl(),
            Resource = resource,
            ExpirationDateTime = DateTimeOffset.UtcNow.Add(SubscriptionLength),
            ClientState = _config.ChatSubscriptionClientStateSecret,
            IncludeResourceData = _crypto.IsEnabled,
            EncryptionCertificate = _crypto.GetPublicCertificateBase64(),
            EncryptionCertificateId = _crypto.EncryptionCertificateId,
        };

        var subscription = await _graphApiClient.EnsureSubscriptionAsync(request, cancellationToken);
        RegisterSubscriptionLocked(subscription);
        return subscription;
    }

    private async Task RememberSubscriptionAsync(
        GraphSubscriptionRecord subscription,
        CancellationToken cancellationToken)
    {
        await _mutex.WaitAsync(cancellationToken);
        try
        {
            RegisterSubscriptionLocked(subscription);
        }
        finally
        {
            _mutex.Release();
        }
    }

    private void RegisterSubscriptionLocked(GraphSubscriptionRecord subscription)
    {
        _subscriptionsById[subscription.Id] = subscription;
        _resourceToSubscriptionId[subscription.Resource] = subscription.Id;
    }

    private void UnregisterSubscriptionLocked(string subscriptionId)
    {
        if (_subscriptionsById.Remove(subscriptionId, out var subscription))
        {
            _resourceToSubscriptionId.Remove(subscription.Resource);
        }
    }

    private void EnsureRenewalLoopStarted()
    {
        if (_renewalLoopTask is null)
        {
            _renewalLoopTask = Task.Run(() => RenewalLoopAsync(_renewalLoopCts.Token));
        }
    }

    private async Task RenewalLoopAsync(CancellationToken cancellationToken)
    {
        using var timer = new PeriodicTimer(RenewalCheckInterval);

        while (await timer.WaitForNextTickAsync(cancellationToken))
        {
            List<GraphSubscriptionRecord> renewals;

            await _mutex.WaitAsync(cancellationToken);
            try
            {
                renewals = _subscriptionsById.Values
                    .Where(subscription => subscription.IsExpiredSoon(RenewalLeadTime))
                    .ToList();
            }
            finally
            {
                _mutex.Release();
            }

            foreach (var subscription in renewals)
            {
                try
                {
                    var renewed = await _graphApiClient.RenewSubscriptionAsync(
                        subscription.Id,
                        DateTimeOffset.UtcNow.Add(SubscriptionLength),
                        cancellationToken);
                    await RememberSubscriptionAsync(renewed, cancellationToken);
                    _logger.LogInformation(
                        "Renewed Graph subscription {SubscriptionId} for resource {Resource}",
                        renewed.Id,
                        renewed.Resource);
                }
                catch (Exception ex)
                {
                    _logger.LogWarning(
                        ex,
                        "Failed to renew Graph subscription {SubscriptionId} for resource {Resource}",
                        subscription.Id,
                        subscription.Resource);
                }
            }
        }
    }

    private string BuildNotificationUrl()
    {
        var baseUrl = _config.GraphNotificationBaseUrl!.TrimEnd('/');
        return $"{baseUrl}/api/graph-notifications";
    }

    private string BuildInstalledChatsResource()
    {
        return $"appCatalogs/teamsApps/{Uri.EscapeDataString(_config.TeamsAppCatalogId!)}/installedToChats/getAllMessages";
    }

    private static string BuildPerChatMessagesResource(string chatThreadId)
    {
        return $"chats/{Uri.EscapeDataString(chatThreadId)}/messages";
    }

    private static string? ExtractChatThreadId(ICall call)
    {
        return call.Resource.ChatInfo?.ThreadId;
    }
}
