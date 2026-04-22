using Microsoft.IdentityModel.Protocols;
using Microsoft.IdentityModel.Protocols.OpenIdConnect;
using Microsoft.IdentityModel.Tokens;
using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using TeamsMediaBot.Models;

namespace TeamsMediaBot.Services;

public sealed class GraphValidationTokenValidator
{
    private const string ExpectedGraphAppId = "0bf30f3b-4a52-48df-9a82-234910c4a086";

    private readonly BotConfiguration _botConfig;
    private readonly ILogger<GraphValidationTokenValidator> _logger;
    private readonly ConfigurationManager<OpenIdConnectConfiguration> _configurationManager;

    public GraphValidationTokenValidator(
        BotConfiguration botConfig,
        ILogger<GraphValidationTokenValidator> logger)
    {
        _botConfig = botConfig;
        _logger = logger;
        _configurationManager = new ConfigurationManager<OpenIdConnectConfiguration>(
            "https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration",
            new OpenIdConnectConfigurationRetriever());
    }

    public async Task<bool> ValidateAsync(IReadOnlyCollection<string>? validationTokens, CancellationToken cancellationToken = default)
    {
        if (validationTokens is null || validationTokens.Count == 0)
        {
            return true;
        }

        var configuration = await _configurationManager.GetConfigurationAsync(cancellationToken);
        var handler = new JwtSecurityTokenHandler();

        foreach (var token in validationTokens)
        {
            if (string.IsNullOrWhiteSpace(token))
            {
                _logger.LogWarning("Skipping blank Graph validation token.");
                return false;
            }

            ClaimsPrincipal principal;
            try
            {
                principal = handler.ValidateToken(
                    token,
                    new TokenValidationParameters
                    {
                        ValidateAudience = true,
                        ValidAudience = _botConfig.AppId,
                        ValidateIssuer = false,
                        ValidateLifetime = true,
                        IssuerSigningKeys = configuration.SigningKeys,
                        ValidateIssuerSigningKey = true,
                    },
                    out _);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Failed to validate Graph notification token.");
                return false;
            }

            var callerAppId = principal.FindFirst("appid")?.Value ?? principal.FindFirst("azp")?.Value;
            if (!string.Equals(callerAppId, ExpectedGraphAppId, StringComparison.OrdinalIgnoreCase))
            {
                _logger.LogWarning(
                    "Rejecting Graph notification token with unexpected caller app id {CallerAppId}",
                    callerAppId);
                return false;
            }
        }

        return true;
    }
}
