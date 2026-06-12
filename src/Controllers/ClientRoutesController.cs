using Microsoft.AspNetCore.Mvc;
using Newtonsoft.Json;
using TeamsMediaBot.Services;

namespace TeamsMediaBot.Controllers;

/// <summary>
/// Operator-facing API for email-based client routing (PLAN.md).
///
/// A client registers their email + sink URL (+ optional client-owned
/// storage container) once. When that person adds Alfred to a meeting,
/// organizes one, or speaks first in its chat, the bot binds the
/// meeting to their route and delivers all subsequent events there —
/// no Teams meeting/chat/team/channel ids required from the client.
/// </summary>
[ApiController]
[Route("api/client-routes")]
public sealed class ClientRoutesController : ControllerBase
{
    private readonly ClientRouteStore _store;
    private readonly ILogger<ClientRoutesController> _logger;

    public ClientRoutesController(ClientRouteStore store, ILogger<ClientRoutesController> logger)
    {
        _store = store;
        _logger = logger;
    }

    [HttpGet]
    [ProducesResponseType(StatusCodes.Status200OK)]
    public IActionResult List()
    {
        var routes = _store.ListRoutes()
            .OrderBy(r => r.Email, StringComparer.Ordinal)
            .ToList();
        return Ok(new { count = routes.Count, routes });
    }

    [HttpPost]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status400BadRequest)]
    public async Task<IActionResult> Upsert(
        [FromBody] UpsertClientRouteRequest request,
        CancellationToken cancellationToken)
    {
        if (request is null || string.IsNullOrWhiteSpace(request.Email))
        {
            return BadRequest(new { error = "email is required" });
        }
        var email = request.Email.Trim();
        if (!email.Contains('@') || email.Any(char.IsWhiteSpace))
        {
            return BadRequest(new { error = $"'{email}' is not a valid email" });
        }
        if (!IsAbsoluteHttpsUrl(request.SinkUrl))
        {
            return BadRequest(new { error = "sink_url must be an absolute HTTPS URL" });
        }
        if (!string.IsNullOrWhiteSpace(request.StorageContainerUrl)
            && !IsAbsoluteHttpsUrl(request.StorageContainerUrl))
        {
            return BadRequest(new { error = "storage_container_url must be an absolute HTTPS URL (container URL with SAS)" });
        }

        var record = new ClientRouteRecord
        {
            Email = ClientRouteRecord.NormalizeEmail(email),
            SinkUrl = request.SinkUrl!.Trim(),
            EventKinds = request.EventKinds is { Count: > 0 }
                ? request.EventKinds
                : new List<string> { "*" },
            Headers = request.Headers,
            StorageContainerUrl = string.IsNullOrWhiteSpace(request.StorageContainerUrl)
                ? null
                : request.StorageContainerUrl!.Trim(),
            Enabled = request.Enabled ?? true,
        };
        await _store.UpsertRouteAsync(record, cancellationToken);

        _logger.LogInformation(
            "Client route upserted email={Email} sink_url={SinkUrl} storage={HasStorage} enabled={Enabled}",
            record.Email, record.SinkUrl, record.StorageContainerUrl is not null, record.Enabled);

        return Ok(_store.GetRoute(record.Email));
    }

    [HttpDelete("{email}")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<IActionResult> Delete(string email, CancellationToken cancellationToken)
    {
        var removed = await _store.RemoveRouteAsync(email, cancellationToken);
        if (!removed)
        {
            return NotFound(new { error = $"no client route for '{email}'" });
        }
        _logger.LogInformation("Client route deleted email={Email}", email);
        return Ok(new { deleted = ClientRouteRecord.NormalizeEmail(email) });
    }

    /// <summary>Meetings currently bound to this client's route.</summary>
    [HttpGet("{email}/meetings")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public IActionResult Meetings(string email)
    {
        if (_store.GetRoute(email) is null)
        {
            return NotFound(new { error = $"no client route for '{email}'" });
        }
        var meetings = _store.ListMeetingRoutes(email)
            .OrderByDescending(m => m.UpdatedAtUtc)
            .ToList();
        return Ok(new { count = meetings.Count, meetings });
    }

    private static bool IsAbsoluteHttpsUrl(string? url) =>
        Uri.TryCreate(url, UriKind.Absolute, out var parsed)
        && string.Equals(parsed.Scheme, Uri.UriSchemeHttps, StringComparison.OrdinalIgnoreCase);
}

public sealed record UpsertClientRouteRequest
{
    [JsonProperty("email")] public string? Email { get; init; }
    [JsonProperty("sink_url")] public string? SinkUrl { get; init; }
    [JsonProperty("event_kinds")] public List<string>? EventKinds { get; init; }
    [JsonProperty("headers")] public Dictionary<string, string>? Headers { get; init; }
    [JsonProperty("storage_container_url")] public string? StorageContainerUrl { get; init; }
    [JsonProperty("enabled")] public bool? Enabled { get; init; }
}
