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
    private readonly PythonChatPublisher _chatPublisher;
    private readonly MeetingChatConfiguration _config;
    private readonly ILogger<GraphNotificationController> _logger;

    public GraphNotificationController(
        PythonChatPublisher chatPublisher,
        MeetingChatConfiguration config,
        ILogger<GraphNotificationController> logger)
    {
        _chatPublisher = chatPublisher;
        _config = config;
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

        // TODO[Alfred]: implement once live-tenant iteration begins.
        //   - Validate clientState against _config.ChatSubscriptionClientStateSecret
        //   - If encryptedContent present: RSA-OAEP unwrap dataKey, AES-CBC decrypt
        //     dataSignature-verified payload, deserialize to chatMessage resource.
        //   - If not encrypted: GET the resource by URL to fetch the chatMessage.
        //   - Handle lifecycleEvent types: reauthorizationRequired, subscriptionRemoved, missed.
        //   - For chatMessage.created/updated: translate to ChatEventPayload and call
        //     _chatPublisher.PublishAsync.
        //   - For chatMessage.deleted: publish with event_type=chat_deleted.

        return Accepted();
    }
}
