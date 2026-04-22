using Microsoft.Graph.Communications.Calls;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

/// <summary>
/// Bridges the Graph Communications Calling SDK to the meeting-chat world.
///
/// On each established call this service should:
///   1. Extract the meeting join URL from the call resource.
///   2. Resolve chatInfo.threadId from Graph (/users/{upn}/onlineMeetings/getByJoinWebUrl).
///   3. Create a Graph change-notification subscription on /chats/{chatId}/messages
///      with encryptionCertificate + lifecycleNotificationUrl + clientState.
///   4. Renew that subscription every ~50 minutes while the call is active.
///   5. Tear down the subscription when the call ends.
///
/// This file is the interface + lifecycle plumbing. The Graph subscription
/// specifics (encryption cert wiring, renewal loop, decryption) are intentionally
/// stubbed with TODOs — they need live-tenant iteration to get right and will
/// land on feat/alfred-chat-modality as a separate commit once Bot Framework
/// messaging (Alfred send path) is proven end-to-end against a real meeting.
/// See docs: https://learn.microsoft.com/en-us/graph/teams-changenotifications-chatmessage
/// </summary>
public interface IMeetingChatService
{
    Task AttachToCallAsync(ICall call, CancellationToken cancellationToken = default);
    Task DetachFromCallAsync(ICall call, CancellationToken cancellationToken = default);

    /// <summary>Return the cached chat thread id for a given call, if resolved.</summary>
    string? GetChatThreadIdForCall(string callId);
}

public sealed class MeetingChatService : IMeetingChatService, IAsyncDisposable
{
    private readonly MeetingChatConfiguration _config;
    private readonly ILogger<MeetingChatService> _logger;
    private readonly Dictionary<string, string> _callToChatThread = new(); // callId -> threadId
    private readonly Dictionary<string, string> _chatThreadToSubscription = new(); // threadId -> graph subscription id
    private readonly SemaphoreSlim _mutex = new(1, 1);
    private bool _disposed;

    public MeetingChatService(
        MeetingChatConfiguration config,
        ILogger<MeetingChatService> logger)
    {
        _config = config;
        _logger = logger;
    }

    public async Task AttachToCallAsync(ICall call, CancellationToken cancellationToken = default)
    {
        if (!_config.Enabled)
        {
            _logger.LogInformation("MeetingChatService disabled via config; not attaching to call {CallId}", call.Id);
            return;
        }

        await _mutex.WaitAsync(cancellationToken);
        try
        {
            var joinUrl = ExtractJoinUrl(call);
            if (string.IsNullOrWhiteSpace(joinUrl))
            {
                _logger.LogWarning("Cannot attach chat subscription — call {CallId} has no resolvable join URL", call.Id);
                return;
            }

            // TODO[Alfred]: Implement once live-tenant iteration begins.
            //   1. var chatThreadId = await _graphClient.Users[organizerUpn]
            //        .OnlineMeetings.GetByJoinWebUrl(joinUrl).GetAsync(cancellationToken);
            //   2. var subscription = await CreateGraphSubscriptionAsync(chatThreadId, cancellationToken);
            //   3. _callToChatThread[call.Id] = chatThreadId;
            //   4. _chatThreadToSubscription[chatThreadId] = subscription.Id;
            //   5. Kick off renewal loop (50-min interval).
            _logger.LogInformation(
                "TODO: create Graph /chats/{{chatId}}/messages subscription for call {CallId} (join={JoinUrl})",
                call.Id, joinUrl);
        }
        finally
        {
            _mutex.Release();
        }
    }

    public async Task DetachFromCallAsync(ICall call, CancellationToken cancellationToken = default)
    {
        await _mutex.WaitAsync(cancellationToken);
        try
        {
            if (!_callToChatThread.TryGetValue(call.Id, out var chatThreadId))
            {
                return;
            }
            _callToChatThread.Remove(call.Id);

            if (_chatThreadToSubscription.TryGetValue(chatThreadId, out var subscriptionId))
            {
                _chatThreadToSubscription.Remove(chatThreadId);
                // TODO[Alfred]: DELETE /subscriptions/{subscriptionId}
                _logger.LogInformation("TODO: tear down Graph subscription {SubscriptionId} for chat {ChatThreadId}",
                    subscriptionId, chatThreadId);
            }
        }
        finally
        {
            _mutex.Release();
        }
    }

    public string? GetChatThreadIdForCall(string callId) =>
        _callToChatThread.TryGetValue(callId, out var thread) ? thread : null;

    private static string? ExtractJoinUrl(ICall call)
    {
        // The join URL is exposed via ICall.Resource (IResource<Call>). Different
        // Calling SDK versions surface it differently; the safest cross-version
        // approach is reflection on the resource, or to grab it from the
        // MeetingInfo / JoinUrl fields when available. Left as TODO to
        // validate against the installed SDK in the target VM.
        return null;
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        await _mutex.WaitAsync();
        _mutex.Release();
        _mutex.Dispose();
    }
}
