using Microsoft.AspNetCore.Mvc;
using Newtonsoft.Json;
using System.Text.Json;
using TeamsMediaBot.Services;

namespace TeamsMediaBot.Controllers;

/// <summary>
/// Read-only debug surface backed by the per-thread NDJSON audit files
/// the bot already writes. Lets operators verify STT is producing text
/// per channel without any database or downstream consumer in the loop.
///
/// Files live at <c>{MeetingAuditLogger.BaseDir}/{sanitized_chat_thread_id}/{transcript|chat|system}.ndjson</c>.
/// </summary>
[ApiController]
[Route("api/debug")]
public sealed class DebugController : ControllerBase
{
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
    };

    private static readonly string[] AllowedKinds = ["transcript", "chat", "system"];

    private readonly MeetingAuditLogger _audit;
    private readonly ILogger<DebugController> _logger;

    public DebugController(MeetingAuditLogger audit, ILogger<DebugController> logger)
    {
        _audit = audit;
        _logger = logger;
    }

    /// <summary>
    /// One row per audited <c>chat_thread_id</c> the bot has ever
    /// written. Includes line counts per stream and the most recent
    /// modification time so the UI can flag "live" threads (last_modified
    /// within the last few seconds).
    /// </summary>
    [HttpGet("transcripts")]
    public IActionResult ListThreads()
    {
        if (!Directory.Exists(_audit.BaseDir))
        {
            return Ok(new { count = 0, base_dir = _audit.BaseDir, threads = Array.Empty<object>() });
        }

        var rows = new List<ThreadSummary>();
        foreach (var dir in Directory.EnumerateDirectories(_audit.BaseDir))
        {
            var sanitized = Path.GetFileName(dir);
            var summary = new ThreadSummary
            {
                ChatThreadIdSanitized = sanitized,
                ChatThreadId = TryRecoverChatThreadId(dir) ?? sanitized,
                TranscriptLines = CountLines(Path.Combine(dir, "transcript.ndjson")),
                ChatLines = CountLines(Path.Combine(dir, "chat.ndjson")),
                SystemLines = CountLines(Path.Combine(dir, "system.ndjson")),
                LastModifiedUtc = NewestMtime(dir),
                FirstFinalText = FirstNonEmptyTranscriptText(Path.Combine(dir, "transcript.ndjson")),
                LastFinalText = LastNonEmptyTranscriptText(Path.Combine(dir, "transcript.ndjson")),
            };
            rows.Add(summary);
        }

        rows.Sort((a, b) =>
            (b.LastModifiedUtc ?? DateTimeOffset.MinValue).CompareTo(a.LastModifiedUtc ?? DateTimeOffset.MinValue));

        return Ok(new
        {
            count = rows.Count,
            base_dir = _audit.BaseDir,
            now_utc = DateTimeOffset.UtcNow,
            threads = rows.Select(r => r.ToWire()).ToList(),
        });
    }

    /// <summary>
    /// Tail of one thread's audit stream. <c>kind</c> is one of
    /// <c>transcript</c> / <c>chat</c> / <c>system</c>; <c>tail</c>
    /// caps the number of lines returned (default 100, max 1000).
    /// </summary>
    [HttpGet("transcripts/{sanitizedChatThreadId}")]
    public IActionResult Tail(
        string sanitizedChatThreadId,
        [FromQuery] string kind = "transcript",
        [FromQuery] int tail = 100)
    {
        if (string.IsNullOrWhiteSpace(sanitizedChatThreadId))
        {
            return BadRequest(new { error = "sanitized_chat_thread_id required" });
        }
        if (!AllowedKinds.Contains(kind))
        {
            return BadRequest(new { error = $"kind must be one of {string.Join(",", AllowedKinds)}" });
        }
        if (tail <= 0 || tail > 1000)
        {
            tail = Math.Clamp(tail, 1, 1000);
        }

        var dir = Path.Combine(_audit.BaseDir, sanitizedChatThreadId);
        var path = Path.Combine(dir, $"{kind}.ndjson");

        // "No audit file yet" is the normal state before the first event of
        // this kind lands — return an empty stream instead of 404 so callers
        // that poll (command center, debug panel) don't have to special-case
        // a missing file vs. a transient error.
        var entries = System.IO.File.Exists(path) ? ReadTail(path, tail) : new List<JsonElement>();

        // Channel reply threads spawn their own audit dir keyed by
        // chat_thread_id;messageid=... — same channel, different
        // conversation. When the caller asks for the bare channel id and
        // the exact match has nothing for this kind, merge in everything
        // from sibling dirs that start with the same prefix so the
        // command-center "Live chat" panel sees every channel reply
        // thread under one view. Sorted by timestamp ascending then
        // tail-capped so we keep the newest N across all threads.
        if (entries.Count < tail && Directory.Exists(_audit.BaseDir))
        {
            var prefix = sanitizedChatThreadId + ";";
            foreach (var siblingDir in Directory.EnumerateDirectories(_audit.BaseDir))
            {
                var name = Path.GetFileName(siblingDir);
                if (!name.StartsWith(prefix, StringComparison.Ordinal)) continue;
                var siblingPath = Path.Combine(siblingDir, $"{kind}.ndjson");
                if (!System.IO.File.Exists(siblingPath)) continue;
                entries.AddRange(ReadTail(siblingPath, tail));
            }
            entries.Sort((a, b) =>
            {
                var aTs = a.TryGetProperty("ts", out var at) && at.ValueKind == JsonValueKind.String
                    ? at.GetString() ?? string.Empty : string.Empty;
                var bTs = b.TryGetProperty("ts", out var bt) && bt.ValueKind == JsonValueKind.String
                    ? bt.GetString() ?? string.Empty : string.Empty;
                return string.CompareOrdinal(aTs, bTs);
            });
            if (entries.Count > tail)
            {
                entries.RemoveRange(0, entries.Count - tail);
            }
        }

        return Ok(new
        {
            chat_thread_id_sanitized = sanitizedChatThreadId,
            chat_thread_id = TryRecoverChatThreadId(dir) ?? sanitizedChatThreadId,
            kind,
            count = entries.Count,
            entries = entries.Select(e => Newtonsoft.Json.Linq.JToken.Parse(e.GetRawText())).ToList(),
        });
    }

    private static long CountLines(string path)
    {
        if (!System.IO.File.Exists(path))
        {
            return 0;
        }
        long n = 0;
        try
        {
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            using var sr = new StreamReader(fs);
            while (sr.ReadLine() is { } _)
            {
                n++;
            }
        }
        catch
        {
            // best-effort
        }
        return n;
    }

    private static DateTimeOffset? NewestMtime(string dir)
    {
        DateTimeOffset? newest = null;
        foreach (var f in Directory.EnumerateFiles(dir))
        {
            var t = System.IO.File.GetLastWriteTimeUtc(f);
            if (newest is null || t > newest.Value.UtcDateTime)
            {
                newest = new DateTimeOffset(t, TimeSpan.Zero);
            }
        }
        return newest;
    }

    private static List<JsonElement> ReadTail(string path, int tail)
    {
        var lines = new List<string>();
        try
        {
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            using var sr = new StreamReader(fs);
            while (sr.ReadLine() is { } line)
            {
                if (string.IsNullOrWhiteSpace(line))
                {
                    continue;
                }
                lines.Add(line);
                if (lines.Count > tail)
                {
                    lines.RemoveAt(0);
                }
            }
        }
        catch
        {
            // best-effort
        }

        var entries = new List<JsonElement>(lines.Count);
        foreach (var l in lines)
        {
            try
            {
                entries.Add(JsonDocument.Parse(l).RootElement.Clone());
            }
            catch
            {
                // skip malformed line
            }
        }
        return entries;
    }

    private static string? FirstNonEmptyTranscriptText(string path) =>
        ScanTranscriptText(path, takeFirst: true);

    private static string? LastNonEmptyTranscriptText(string path) =>
        ScanTranscriptText(path, takeFirst: false);

    private static string? ScanTranscriptText(string path, bool takeFirst)
    {
        if (!System.IO.File.Exists(path))
        {
            return null;
        }
        string? hit = null;
        try
        {
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            using var sr = new StreamReader(fs);
            while (sr.ReadLine() is { } line)
            {
                if (string.IsNullOrWhiteSpace(line)) continue;
                using var doc = JsonDocument.Parse(line);
                var root = doc.RootElement;
                if (!root.TryGetProperty("event_type", out var et) ||
                    et.ValueKind != JsonValueKind.String ||
                    et.GetString() != "transcript.final")
                {
                    continue;
                }
                if (!root.TryGetProperty("payload", out var payload) ||
                    !payload.TryGetProperty("text", out var text) ||
                    text.ValueKind != JsonValueKind.String)
                {
                    continue;
                }
                var t = text.GetString();
                if (string.IsNullOrWhiteSpace(t)) continue;
                hit = t;
                if (takeFirst) break;
            }
        }
        catch
        {
            // best-effort
        }
        return hit;
    }

    /// <summary>
    /// Recovers the original <c>chat_thread_id</c> from an audited line
    /// inside the directory. The directory name is sanitized (colons
    /// replaced with underscores) so we read the first event's
    /// <c>chat_thread_id</c> field to get the canonical form back.
    /// </summary>
    private static string? TryRecoverChatThreadId(string dir)
    {
        foreach (var name in new[] { "transcript.ndjson", "chat.ndjson", "system.ndjson" })
        {
            var path = Path.Combine(dir, name);
            if (!System.IO.File.Exists(path)) continue;
            try
            {
                using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
                using var sr = new StreamReader(fs);
                if (sr.ReadLine() is { } first && !string.IsNullOrWhiteSpace(first))
                {
                    using var doc = JsonDocument.Parse(first);
                    if (doc.RootElement.TryGetProperty("chat_thread_id", out var v) &&
                        v.ValueKind == JsonValueKind.String)
                    {
                        return v.GetString();
                    }
                }
            }
            catch
            {
                // best-effort
            }
        }
        return null;
    }

    private sealed record ThreadSummary
    {
        public required string ChatThreadIdSanitized { get; init; }
        public required string ChatThreadId { get; init; }
        public long TranscriptLines { get; init; }
        public long ChatLines { get; init; }
        public long SystemLines { get; init; }
        public DateTimeOffset? LastModifiedUtc { get; init; }
        public string? FirstFinalText { get; init; }
        public string? LastFinalText { get; init; }

        // Newtonsoft.Json (the controllers' configured serializer) defaults
        // to camelCase property names without explicit attributes, but the
        // UI + curl tooling all expect snake_case. Project to a wire DTO
        // so we get snake_case keys without sprinkling [JsonProperty] on
        // an internal record.
        public object ToWire() => new
        {
            chat_thread_id_sanitized = ChatThreadIdSanitized,
            chat_thread_id = ChatThreadId,
            transcript_lines = TranscriptLines,
            chat_lines = ChatLines,
            system_lines = SystemLines,
            last_modified_utc = LastModifiedUtc,
            first_final_text = FirstFinalText,
            last_final_text = LastFinalText,
        };
    }
}
