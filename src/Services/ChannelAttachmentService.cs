using Microsoft.Bot.Builder;
using Microsoft.Bot.Schema;
using Microsoft.Extensions.Hosting;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

public interface IChannelAttachmentService
{
    Task AttachAsync(
        ChannelAttachmentRequest request,
        CancellationToken cancellationToken = default);

    Task<bool> DetachAsync(
        string teamId,
        string channelId,
        CancellationToken cancellationToken = default);

    IReadOnlyList<ChannelAttachmentRecord> List();

    ChannelAttachmentRecord? Get(string teamId, string channelId);

    ChannelAttachmentRecord? GetByConversationThreadId(string conversationThreadId);

    Task<bool> SetConsumersAsync(
        string teamId,
        string channelId,
        IReadOnlyList<ConsumerConfig> consumers,
        CancellationToken cancellationToken = default);

    Task<bool> UpsertConsumerAsync(
        string teamId,
        string channelId,
        ConsumerConfig consumer,
        CancellationToken cancellationToken = default);

    Task<bool> RemoveConsumerAsync(
        string teamId,
        string channelId,
        string consumerName,
        CancellationToken cancellationToken = default);
}

/// <summary>
/// Caller-supplied attach request. <see cref="ChannelDisplayName"/> and
/// <see cref="TeamDisplayName"/> are best-effort labels for the operator UI;
/// <see cref="Source"/> is a free-form tag (e.g. <c>"team_install"</c>,
/// <c>"manual_attach"</c>) to make it easy to see how each binding came in.
/// </summary>
public sealed record ChannelAttachmentRequest
{
    public required string TeamId { get; init; }
    public required string ChannelId { get; init; }
    public string? ConversationThreadId { get; init; }
    public string? ChannelDisplayName { get; init; }
    public string? TeamDisplayName { get; init; }
    public string? ServiceUrl { get; init; }
    public string? TenantId { get; init; }
    public string? Source { get; init; }
}

/// <summary>
/// Orchestrates persistent channel attachment:
/// <list type="bullet">
///   <item>Persists the (teamId, channelId) binding via <see cref="ChannelAttachmentStore"/>.</item>
///   <item>Creates a Graph change-notification subscription on
///         <c>teams/{teamId}/channels/{channelId}/messages</c> via
///         <see cref="IMeetingChatService"/>, so Alfred receives every
///         channel post (including ones not @-mentioning the bot).</item>
///   <item>On startup re-issues subscriptions for every persisted
///         attachment, so attachment is genuinely persistent across bot
///         restarts.</item>
/// </list>
/// </summary>
public sealed class ChannelAttachmentService : IChannelAttachmentService, IHostedService
{
    private const string BootstrapDefaultConsumerName = "bootstrap-default";

    private readonly ChannelAttachmentStore _store;
    private readonly IMeetingChatService _meetingChatService;
    private readonly EventDispatchConfiguration _dispatchConfig;
    private readonly ILogger<ChannelAttachmentService> _logger;

    public ChannelAttachmentService(
        ChannelAttachmentStore store,
        IMeetingChatService meetingChatService,
        EventDispatchConfiguration dispatchConfig,
        ILogger<ChannelAttachmentService> logger)
    {
        _store = store;
        _meetingChatService = meetingChatService;
        _dispatchConfig = dispatchConfig;
        _logger = logger;
    }

    public async Task StartAsync(CancellationToken cancellationToken)
    {
        await _store.LoadAsync(cancellationToken);

        foreach (var record in _store.List())
        {
            try
            {
                var subscription = await _meetingChatService
                    .EnsureChannelMessagesSubscriptionAsync(record.TeamId, record.ChannelId, cancellationToken);

                var refreshed = record with
                {
                    SubscriptionId = subscription.SubscriptionId,
                    SubscriptionResource = subscription.Resource,
                    SubscriptionExpiresAtUtc = subscription.ExpiresAtUtc,
                };

                if (ShouldApplyBootstrapSeed(refreshed))
                {
                    refreshed = refreshed with
                    {
                        Consumers = new[] { BuildBootstrapDefaultConsumer(_dispatchConfig.BootstrapConsumerUrl!) },
                        BootstrapSeeded = true,
                    };
                    _logger.LogInformation(
                        "Seeded bootstrap-default consumer on existing attachment TeamId={TeamId} ChannelId={ChannelId} Url={Url}",
                        refreshed.TeamId, refreshed.ChannelId, _dispatchConfig.BootstrapConsumerUrl);
                }

                await _store.UpsertAsync(refreshed, cancellationToken);

                _logger.LogInformation(
                    "Restored channel subscription on startup TeamId={TeamId} ChannelId={ChannelId} SubscriptionId={SubscriptionId}",
                    record.TeamId,
                    record.ChannelId,
                    subscription.SubscriptionId);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(
                    ex,
                    "Failed to restore channel subscription on startup TeamId={TeamId} ChannelId={ChannelId}; the binding stays attached and will retry on the next attach call.",
                    record.TeamId,
                    record.ChannelId);
            }
        }
    }

    private bool ShouldApplyBootstrapSeed(ChannelAttachmentRecord record) =>
        !record.BootstrapSeeded
        && record.Consumers.Count == 0
        && !string.IsNullOrWhiteSpace(_dispatchConfig.BootstrapConsumerUrl);

    private static ConsumerConfig BuildBootstrapDefaultConsumer(string url) =>
        new()
        {
            Name = BootstrapDefaultConsumerName,
            Url = url,
            EventKinds = new[] { "*" },
            Enabled = true,
        };

    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;

    public async Task AttachAsync(
        ChannelAttachmentRequest request,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentException.ThrowIfNullOrWhiteSpace(request.TeamId);
        ArgumentException.ThrowIfNullOrWhiteSpace(request.ChannelId);

        var existing = _store.Get(request.TeamId, request.ChannelId);
        var subscription = await _meetingChatService.EnsureChannelMessagesSubscriptionAsync(
            request.TeamId,
            request.ChannelId,
            cancellationToken);

        var consumers = existing?.Consumers ?? Array.Empty<ConsumerConfig>();
        var bootstrapSeeded = existing?.BootstrapSeeded ?? false;

        if (existing is null
            && consumers.Count == 0
            && !string.IsNullOrWhiteSpace(_dispatchConfig.BootstrapConsumerUrl))
        {
            consumers = new[] { BuildBootstrapDefaultConsumer(_dispatchConfig.BootstrapConsumerUrl!) };
            bootstrapSeeded = true;
            _logger.LogInformation(
                "Seeded bootstrap-default consumer on new attachment TeamId={TeamId} ChannelId={ChannelId} Url={Url}",
                request.TeamId, request.ChannelId, _dispatchConfig.BootstrapConsumerUrl);
        }

        var record = new ChannelAttachmentRecord
        {
            TeamId = request.TeamId,
            ChannelId = request.ChannelId,
            ConversationThreadId = request.ConversationThreadId
                ?? existing?.ConversationThreadId,
            ChannelDisplayName = request.ChannelDisplayName ?? existing?.ChannelDisplayName,
            TeamDisplayName = request.TeamDisplayName ?? existing?.TeamDisplayName,
            ServiceUrl = request.ServiceUrl ?? existing?.ServiceUrl,
            TenantId = request.TenantId ?? existing?.TenantId,
            AttachedAtUtc = existing?.AttachedAtUtc ?? DateTimeOffset.UtcNow,
            Source = request.Source ?? existing?.Source,
            SubscriptionId = subscription.SubscriptionId,
            SubscriptionResource = subscription.Resource,
            SubscriptionExpiresAtUtc = subscription.ExpiresAtUtc,
            Consumers = consumers,
            BootstrapSeeded = bootstrapSeeded,
        };

        await _store.UpsertAsync(record, cancellationToken);
    }

    public async Task<bool> DetachAsync(
        string teamId,
        string channelId,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(teamId);
        ArgumentException.ThrowIfNullOrWhiteSpace(channelId);

        await _meetingChatService.DeleteChannelMessagesSubscriptionAsync(teamId, channelId, cancellationToken);
        return await _store.RemoveAsync(teamId, channelId, cancellationToken);
    }

    public IReadOnlyList<ChannelAttachmentRecord> List() => _store.List();

    public ChannelAttachmentRecord? Get(string teamId, string channelId) =>
        _store.Get(teamId, channelId);

    public ChannelAttachmentRecord? GetByConversationThreadId(string conversationThreadId) =>
        _store.GetByConversationThreadId(conversationThreadId);

    public Task<bool> SetConsumersAsync(
        string teamId,
        string channelId,
        IReadOnlyList<ConsumerConfig> consumers,
        CancellationToken cancellationToken = default) =>
        _store.SetConsumersAsync(teamId, channelId, consumers, cancellationToken);

    public Task<bool> UpsertConsumerAsync(
        string teamId,
        string channelId,
        ConsumerConfig consumer,
        CancellationToken cancellationToken = default) =>
        _store.UpsertConsumerAsync(teamId, channelId, consumer, cancellationToken);

    public Task<bool> RemoveConsumerAsync(
        string teamId,
        string channelId,
        string consumerName,
        CancellationToken cancellationToken = default) =>
        _store.RemoveConsumerAsync(teamId, channelId, consumerName, cancellationToken);
}
