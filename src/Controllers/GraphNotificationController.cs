using Microsoft.AspNetCore.Mvc;
using TeamsMediaBot.Models;
using TeamsMediaBot.Services;

namespace TeamsMediaBot.Controllers;

/// <summary>
/// Graph change-notification receiver for meeting-chat subscriptions.
///
/// Handles three things:
///   - Validation handshake (10-second echo of validationToken) on creation.
///   - Lifecycle events (reauthorizationRequired / subscriptionRemoved / missed).
///   - Delivery of chatMessage resource data: decrypt (if includeResourceData),
///     translate to ChatEventPayload, POST to the Python sink via
///     PythonChatPublisher.
///
/// This file has the correct endpoint wiring + validation handshake. The
/// encrypted-notification decrypt path is marked TODO for live-tenant
/// iteration (needs a real RSA cert and tenant to test against).
/// See docs: https://learn.microsoft.com/en-us/graph/change-notifications-with-resource-data
/// </summary>
[ApiController]
[Route("api/graph-notifications")]
public sealed class GraphNotificationController : ControllerBase
{
    private readonly GraphNotificationProcessor _processor;
    private readonly ILogger<GraphNotificationController> _logger;

    public GraphNotificationController(
        GraphNotificationProcessor processor,
        ILogger<GraphNotificationController> logger)
    {
        _processor = processor;
        _logger = logger;
    }

    [HttpPost]
    public async Task<IActionResult> PostAsync(
        [FromQuery(Name = "validationToken")] string? validationToken,
        CancellationToken ct)
    {
        // Step 1: Graph subscription validation handshake.
        if (!string.IsNullOrEmpty(validationToken))
        {
            _logger.LogInformation("Received Graph validation token; echoing back.");
            return Content(validationToken, "text/plain");
        }

        // Step 2: Parse the notification envelope.
        using var reader = new StreamReader(Request.Body);
        var body = await reader.ReadToEndAsync(ct);
        _logger.LogDebug("Graph notification body ({Bytes} bytes): {Preview}",
            body.Length, body.Length > 200 ? body[..200] : body);

        _ = Task.Run(
            async () =>
            {
                try
                {
                    await _processor.ProcessAsync(body, CancellationToken.None);
                }
                catch (Exception ex)
                {
                    _logger.LogError(ex, "Unhandled error while processing Graph notification batch.");
                }
            });

        return Accepted();
    }
}
