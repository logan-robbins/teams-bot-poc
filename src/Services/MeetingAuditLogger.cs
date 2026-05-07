using System.Collections.Concurrent;
using System.Text;
using System.Text.Json;

namespace TeamsMediaBot.Services;

/// <summary>
/// Appends raw meeting events as NDJSON to per-meeting files on the VM.
///
/// Layout: {BaseDir}\{sanitized_chat_thread_id}\{stream}.ndjson
///   e.g.  C:\teams-bot-poc\meeting-logs\19_meeting_xxx_thread.v2\transcript.ndjson
///
/// Each line is a single JSON object with a utc_written timestamp prepended so
/// the file is self-describing without needing the service log.
/// </summary>
public sealed class MeetingAuditLogger
{
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly string _baseDir;

    // One writer (StreamWriter) per meeting+stream key so we hold the file open
    // across events rather than reopening on every write.
    private readonly ConcurrentDictionary<string, StreamWriter> _writers = new();

    public MeetingAuditLogger(string baseDir)
    {
        _baseDir = baseDir;
    }

    /// <summary>Absolute path to the directory holding per-thread audit files.</summary>
    public string BaseDir => _baseDir;

    public void Append(string chatThreadId, string stream, object payload)
    {
        if (string.IsNullOrWhiteSpace(chatThreadId))
            return;

        var writer = _writers.GetOrAdd(
            $"{chatThreadId}|{stream}",
            _ => OpenWriter(chatThreadId, stream));

        var line = JsonSerializer.Serialize(payload, JsonOpts);
        lock (writer)
        {
            writer.WriteLine(line);
            writer.Flush();
        }
    }

    private StreamWriter OpenWriter(string chatThreadId, string stream)
    {
        var dir = Path.Combine(_baseDir, Sanitize(chatThreadId));
        Directory.CreateDirectory(dir);
        var path = Path.Combine(dir, $"{stream}.ndjson");
        return new StreamWriter(path, append: true, encoding: Encoding.UTF8);
    }

    // Replace characters that are illegal in Windows paths with underscores.
    private static string Sanitize(string id)
    {
        var invalid = Path.GetInvalidFileNameChars();
        var sb = new System.Text.StringBuilder(id.Length);
        foreach (var c in id)
            sb.Append(Array.IndexOf(invalid, c) >= 0 ? '_' : c);
        return sb.ToString();
    }
}
