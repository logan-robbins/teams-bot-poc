using Microsoft.AspNetCore.Mvc;
using Microsoft.Bot.Builder;
using Microsoft.Bot.Builder.Integration.AspNet.Core;

namespace TeamsMediaBot.Controllers;

/// <summary>
/// Bot Framework /api/messages endpoint.
///
/// Teams delivers every inbound chat activity here (because the app's
/// bot messaging endpoint is configured against this URL in Azure Bot
/// Service). The CloudAdapter routes the activity to AlfredBot which
/// captures the ConversationReference for proactive sends and forwards
/// the message to the Python sink.
/// </summary>
[ApiController]
[Route("api/messages")]
public sealed class MessagesController : ControllerBase
{
    private readonly IBotFrameworkHttpAdapter _adapter;
    private readonly IBot _bot;

    public MessagesController(IBotFrameworkHttpAdapter adapter, IBot bot)
    {
        _adapter = adapter;
        _bot = bot;
    }

    [HttpPost]
    public Task PostAsync() => _adapter.ProcessAsync(Request, Response, _bot);
}
