using System.Collections.Concurrent;
using Microsoft.Bot.Schema;

namespace TeamsMediaBot.Services;

/// <summary>
/// Process-local cache of Bot Framework ConversationReferences keyed by chat thread id.
///
/// Bot Framework proactive messaging (the only supported 2026 path for an
/// application-permission bot to post into a meeting chat) requires a
/// captured ConversationReference. We grab the reference from the first
/// activity the Bot Framework adapter delivers for a given chat and keep
/// it here until the meeting ends.
///
/// Not durable: a process restart loses all refs until the bot sees a
/// fresh chat event for each active meeting. Revisit with Redis if we
/// scale beyond a single process.
/// </summary>
public interface IConversationReferenceStore
{
    void Put(string chatThreadId, ConversationReference reference);
    ConversationReference? Get(string chatThreadId);
    bool Remove(string chatThreadId);
    IReadOnlyCollection<string> KnownChatThreadIds { get; }
}

public sealed class InMemoryConversationReferenceStore : IConversationReferenceStore
{
    private readonly ConcurrentDictionary<string, ConversationReference> _refs = new();

    public void Put(string chatThreadId, ConversationReference reference)
    {
        if (string.IsNullOrWhiteSpace(chatThreadId))
            throw new ArgumentException("chatThreadId must be non-empty", nameof(chatThreadId));
        _refs[chatThreadId] = reference ?? throw new ArgumentNullException(nameof(reference));
    }

    public ConversationReference? Get(string chatThreadId) =>
        _refs.TryGetValue(chatThreadId, out var reference) ? reference : null;

    public bool Remove(string chatThreadId) => _refs.TryRemove(chatThreadId, out _);

    public IReadOnlyCollection<string> KnownChatThreadIds => _refs.Keys.ToList();
}
